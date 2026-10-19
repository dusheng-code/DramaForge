"""职责：七个 role 的 OpenAI 兼容端点客户端（§8）：统一结构化输出、`json_schema → function_calling → json_mode` 三级降级、可重试性区分与日志脱敏。
输入：role 名（读 `LLM_{ROLE}_*`）、system/user 文本、目标 Pydantic 模型（或 `list[Model]`）、可选 key 用于区分子用途。
输出：校验后的结构化对象、纯文本回复；未配置抛 LLMNotConfigured，调用/解析失败抛 LLMError。

副作用：发 HTTPS 请求（httpx.AsyncClient，直连 `{base_url}/chat/completions`，不引 openai SDK）、读 Settings、写 loguru 日志。
禁止：日志中出现 api_key 或完整 prompt 正文（只记录 role/model/长度/耗时/状态码）。
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from typing import Any, Awaitable, Callable

import httpx
from loguru import logger

from ..core.config import RoleLLMConfig, Settings, get_settings

# ── 结构化输出降级顺序（§8：逐级 try，上一级不支持就降级）──────────────
STRUCTURED_MODES: tuple[str, ...] = ("json_schema", "function_calling", "json_mode")

# §8 要求结构化调用强制关闭 thinking。具体字段名各端点不一致（MiniMax / OpenAI 兼容网关），
# 待 §14 核实后只改这一处常量；在核实前统一发这个形状。
DISABLE_THINKING_BODY: dict[str, Any] = {"thinking": {"type": "disabled"}}

# 重试与超时（网络/429/5xx 才重试；其他 4xx 直接抛错）
MAX_ATTEMPTS: int = 3
RETRY_BASE_DELAY_SEC: float = 0.5
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
# 这些 4xx/5xx 说明「该结构化模式不被支持」，值得降级到下一级模式
DEGRADE_STATUS_CODES: frozenset[int] = frozenset({400, 404, 405, 415, 422, 501})
DEGRADE_HINTS: tuple[str, ...] = (
    "not support",
    "unsupported",
    "unknown parameter",
    "unknown field",
    "invalid_request_error",
    "response_format",
    "json_schema",
    "tool_choice",
    "tools",
    "thinking",
)
REQUEST_TIMEOUT_SEC: float = 120.0
CHAT_COMPLETIONS_PATH: str = "/chat/completions"
JSON_ONLY_SUFFIX: str = "\n\n只输出一个 JSON 对象/数组本身：不要 Markdown 代码块，不要任何解释文字。"
_ERROR_BRIEF_LIMIT: int = 200

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """LLM 调用的统一异常。`status_code` 供降级判断，`retryable` 供 with_retry 判断。"""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable

    def __str__(self) -> str:
        """异常文本带上状态码，便于日志与降级判据。无副作用。"""
        base = super().__str__()
        return f"{base}（status={self.status_code}）" if self.status_code is not None else base


class LLMNotConfigured(LLMError):
    """该 role 未配置 api_key / base_url / model，无法真实调用（配置问题，不可重试）。"""


def with_retry(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """模块级异步重试装饰器：网络错误/429/5xx 指数退避最多 3 次，其他 4xx 立即抛 LLMError。

    副作用：`asyncio.sleep` 等待与 loguru 告警日志（只记异常类型与状态码，不记请求正文）。
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        """按可重试性执行 func；失败时指数退避重试，耗尽后抛 LLMError。副作用：sleep 与告警日志。"""
        delay = RETRY_BASE_DELAY_SEC
        last_exc: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await func(*args, **kwargs)
            except LLMNotConfigured:
                raise
            except LLMError as exc:
                if not exc.retryable or attempt >= MAX_ATTEMPTS:
                    raise
                last_exc = exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise LLMError(
                        f"LLM 网络错误重试 {MAX_ATTEMPTS} 次仍失败：{type(exc).__name__}",
                        retryable=True,
                    ) from exc
                last_exc = exc
            logger.warning(
                "LLM 调用失败，{}s 后重试（{}/{}）：{}",
                delay,
                attempt,
                MAX_ATTEMPTS,
                _reason_brief(last_exc),
            )
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError(f"LLM 重试耗尽：{_reason_brief(last_exc)}") from last_exc

    return wrapper


class LLMClient:
    """单个 role 的 OpenAI 兼容客户端。实例无状态，可并发复用（每次调用自建 httpx 连接）。"""

    def __init__(self, role: str, config: RoleLLMConfig, *, timeout: float = REQUEST_TIMEOUT_SEC) -> None:
        self.role = role
        self.config = config
        self.timeout = timeout

    # ── 构造与自检 ─────────────────────────────────────────
    @classmethod
    def from_role(cls, role: str, settings: Settings | None = None) -> "LLMClient":
        """按 role 读 `LLM_{ROLE}_*` 构造客户端（缺项不报错，调用时由 configured 判定）。无副作用。"""
        s = settings or get_settings()
        return cls(role=role, config=s.role_llm(role))

    @property
    def configured(self) -> bool:
        """是否具备真实调用条件（api_key 与 base_url 均非空）。无副作用。"""
        return bool(self.config.api_key) and bool(self.config.base_url)

    @property
    def url(self) -> str:
        """chat/completions 完整地址（`{base_url}/chat/completions`）。无副作用。"""
        return self.config.base_url.rstrip("/") + CHAT_COMPLETIONS_PATH

    def _ensure_ready(self) -> None:
        """调用前自检：缺 key/base_url/model 抛 LLMNotConfigured（消息可直接给人看）。无副作用。"""
        env_prefix = f"LLM_{self.role.upper()}"
        if not self.configured:
            raise LLMNotConfigured(
                f"role='{self.role}' 未配置 API Key 或 Base URL：请设置 {env_prefix}_API_KEY 与 "
                f"{env_prefix}_BASE_URL 后重试（当前 key={'有' if self.config.api_key else '空'}，"
                f"base_url={'有' if self.config.base_url else '空'}）。"
            )
        if not self.config.model:
            raise LLMNotConfigured(
                f"role='{self.role}' 未配置模型名：请设置 {env_prefix}_MODEL 后重试。"
            )

    # ── 对外方法 ───────────────────────────────────────────
    async def agenerate_text(self, system: str, user: str) -> str:
        """纯文本生成（用于单集剧本 md 等）。副作用：一次 HTTPS 请求 + info 日志。"""
        self._ensure_ready()
        started = time.perf_counter()
        payload = self._base_payload(system, user)
        data = await self._post(payload)
        text = (_message_of(data).get("content") or "").strip()
        logger.info(
            "llm.text role={} model={} system_len={} user_len={} out_len={} elapsed_ms={}",
            self.role,
            self.config.model,
            len(system),
            len(user),
            len(text),
            int((time.perf_counter() - started) * 1000),
        )
        return text

    async def agenerate_object(
        self,
        system: str,
        user: str,
        model_cls: Any,
        key: str | None = None,
    ) -> Any:
        """结构化生成：按 `json_schema → function_calling → json_mode` 逐级降级，返回校验后的模型。

        `key` 用于区分同一 role 的子用途（如 `script.outline` / `script.episode` / `script.characters`）。
        副作用：1~3 次 HTTPS 请求；每次降级记 info 日志说明降级到哪一级。
        """
        self._ensure_ready()
        # 惰性 import：objects 反向依赖本模块的 LLMError，顶层互引会成环
        from .objects import schema_name  # noqa: PLC0415 —— 见上

        name = key or self.role
        last_exc: LLMError | None = None
        for index, mode in enumerate(STRUCTURED_MODES):
            payload = self._structured_payload(mode, system, user, model_cls, name)
            started = time.perf_counter()
            try:
                data = await self._post(payload)
                raw = self._extract_structured(mode, data)
                obj = self._parse(model_cls, raw)
            except LLMError as exc:
                if not _should_degrade(exc):
                    raise
                last_exc = exc
                next_mode = STRUCTURED_MODES[index + 1] if index + 1 < len(STRUCTURED_MODES) else None
                logger.info(
                    "llm 结构化输出降级：role={} key={} 由 {} 降级到 {}（原因：{}）",
                    self.role,
                    name,
                    mode,
                    next_mode or "无（三级已用尽，将汇总报错）",
                    _reason_brief(exc),
                )
                continue
            logger.info(
                "llm.object role={} key={} model={} schema={} mode={} system_len={} user_len={} elapsed_ms={}",
                self.role,
                name,
                self.config.model,
                schema_name(model_cls),
                mode,
                len(system),
                len(user),
                int((time.perf_counter() - started) * 1000),
            )
            return obj
        raise LLMError(
            f"role='{self.role}' 的结构化输出三级模式全部失败（{' → '.join(STRUCTURED_MODES)}）："
            f"{_reason_brief(last_exc)}"
        ) from last_exc

    # ── 请求构造 ───────────────────────────────────────────
    def _base_payload(self, system: str, user: str) -> dict[str, Any]:
        """组装 messages 与采样参数；并入「关闭 thinking」参数（httpx 直连，等价 extra_body）。无副作用。"""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(self.config.temperature),
            "max_tokens": int(self.config.max_tokens),
            "stream": False,
        }
        payload.update(DISABLE_THINKING_BODY)
        return payload

    def _structured_payload(
        self,
        mode: str,
        system: str,
        user: str,
        model_cls: Any,
        name: str,
    ) -> dict[str, Any]:
        """按结构化模式补 response_format / tools；json_mode 额外在 user 尾部加「只输出 JSON」。无副作用。"""
        payload = self._base_payload(system, user)
        if mode == "json_schema":
            from .objects import json_schema_response_format  # 惰性 import，避免循环依赖

            payload["response_format"] = json_schema_response_format(model_cls, name)
            _append_user_suffix(payload, JSON_ONLY_SUFFIX)
        elif mode == "function_calling":
            from .objects import response_schema  # 惰性 import，避免循环依赖

            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "按 parameters 给出的 JSON Schema 提交结构化结果。",
                        "parameters": response_schema(model_cls),
                    },
                }
            ]
            payload["tool_choice"] = {"type": "function", "function": {"name": name}}
        else:  # json_mode：只靠提示词约束 JSON，兼容性最好但 schema 保障最弱
            payload["response_format"] = {"type": "json_object"}
            _append_user_suffix(payload, JSON_ONLY_SUFFIX)
        return payload

    # ── HTTP ───────────────────────────────────────────────
    @with_retry
    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST 一次 chat/completions：4xx/5xx 转 LLMError（标好 retryable），网络异常交给 with_retry。

        副作用：一次 HTTPS 请求。日志不输出 api_key，错误文本只截断 200 字符。
        """
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise LLMError(
                f"LLM HTTP {resp.status_code}（role={self.role}）：{_brief(resp.text)}",
                status_code=resp.status_code,
                retryable=resp.status_code in RETRYABLE_STATUS_CODES,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(
                f"LLM 返回非 JSON 响应（role={self.role}，HTTP {resp.status_code}）",
                status_code=resp.status_code,
            ) from exc

    # ── 回包解析 ───────────────────────────────────────────
    def _extract_structured(self, mode: str, data: dict[str, Any]) -> Any:
        """从回包取结构化载荷：function_calling 取 tool_calls.arguments，其余取 content 文本。无副作用。"""
        message = _message_of(data)
        if mode == "function_calling":
            calls = message.get("tool_calls") or []
            if not calls:
                # 该端点不认 tools/tool_choice：标 422 以触发降级到 json_mode
                raise LLMError(
                    f"role='{self.role}' 的 function_calling 回包缺少 tool_calls",
                    status_code=422,
                )
            function = (calls[0] or {}).get("function") or {}
            arguments = function.get("arguments")
            return arguments if arguments is not None else {}
        content = message.get("content")
        if content is None:
            raise LLMError(f"role='{self.role}' 的回包没有 content 字段", status_code=422)
        return content

    def _parse(self, model_cls: Any, raw: Any) -> Any:
        """把结构化载荷落成模型；dict 直通，str 走健壮 JSON 提取。副作用：无（失败抛 LLMError）。"""
        from .objects import parse_into  # 惰性 import，避免循环依赖

        if isinstance(raw, (dict, list)):
            return parse_into(model_cls, raw)
        return parse_into(model_cls, parse_json_text(str(raw)))


# ── 模块级工具 ─────────────────────────────────────────────
def parse_json_text(text: str) -> Any:
    """从模型输出提取 JSON：容忍 ```json 代码块与前后废话；失败抛不可重试的 LLMError。无副作用。"""
    if text is None:
        raise LLMError("LLM 返回空内容，无法解析 JSON（不可重试）")
    raw = str(text).strip()
    if not raw:
        raise LLMError("LLM 返回空内容，无法解析 JSON（不可重试）")
    candidates: list[str] = []
    fence = _FENCE_RE.search(raw)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(raw)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            pass
        span = _find_json_span(candidate)
        if span is None:
            continue
        try:
            return json.loads(span)
        except ValueError:
            continue
    raise LLMError("LLM 输出无法解析为 JSON（不可重试）")


def _find_json_span(text: str) -> str | None:
    """扫描第一个 `{` / `[` 并配平到对应闭合符（尊重字符串与转义）。无副作用。"""
    start = -1
    opener = ""
    for idx, ch in enumerate(text):
        if ch in "{[":
            start = idx
            opener = ch
            break
    if start < 0:
        return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _append_user_suffix(payload: dict[str, Any], suffix: str) -> None:
    """在最后一条 user 消息尾部追加约束（就地修改 payload）。副作用：入参 dict 被修改。"""
    messages = payload.get("messages") or []
    for message in reversed(messages):
        if message.get("role") == "user":
            message["content"] = f"{message.get('content') or ''}{suffix}"
            return


def _message_of(data: dict[str, Any]) -> dict[str, Any]:
    """取 `choices[0].message`；结构异常时抛不可重试 LLMError（不回显整包）。无副作用。"""
    try:
        choices = data.get("choices") or []
        message = (choices[0] or {}).get("message") or {}
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM 回包结构异常：{type(exc).__name__}（不可重试）") from exc
    if not message:
        raise LLMError("LLM 回包缺少 choices[0].message（不可重试）")
    return message


def _should_degrade(exc: LLMError) -> bool:
    """判断某级结构化模式失败是否值得降级到下一级（4xx/不支持 → 降级；鉴权/解析失败 → 不降级）。无副作用。"""
    if exc.status_code is not None and exc.status_code in DEGRADE_STATUS_CODES:
        return True
    lowered = str(exc).lower()
    return any(hint in lowered for hint in DEGRADE_HINTS)


def _brief(text: str, limit: int = _ERROR_BRIEF_LIMIT) -> str:
    """截断外部错误文本，避免把大段回包（可能含 prompt 片段）灌进日志。无副作用。"""
    flat = " ".join((text or "").split())
    return flat[:limit]


def _reason_brief(exc: BaseException | None) -> str:
    """异常 → 一行可读原因（类型 + 状态码），绝不带请求正文。无副作用。"""
    if exc is None:
        return "未知原因"
    if isinstance(exc, LLMError):
        return f"{type(exc).__name__}(status={exc.status_code})：{_brief(str(exc))}"
    return f"{type(exc).__name__}：{_brief(str(exc))}"
