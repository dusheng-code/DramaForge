"""职责：视频 provider 抽象与 MiniMax 视频客户端（§10.4）：提交/轮询/下载，并把 §14.1/14.2/14.3/14.4/14.7 未核实的字段名收口成模块顶部一张 MINIMAX_FIELD_MAP。
输入：VideoRequest、MiniMax 回执 JSON、Settings 的 MINIMAX_* / POLL_* / DOWNLOAD_TIMEOUT_SEC。
输出：submit → provider_task_id；poll → VideoOutcome（done/failed/pending 三态 + cost_cny + url）；download_bytes → 字节。

副作用：MiniMaxVideoClient / download_bytes 发 HTTPS 请求（唯一允许真实 HTTP 的地方）；不写磁盘、不建目录。
禁止：启动期发网络请求；日志打印 API Key 或带凭据的完整 URL（只打 host）。
"""

from __future__ import annotations

import math
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from loguru import logger

from ..contracts.assets import VideoOutcome, VideoRequest
from ..core.config import Settings
from . import pricing
from .fake_video import FakeVideoClient

# ══════════════════════════════════════════════════════════════════════════════
# MiniMax 字段名总表（§14.1/14.2/14.3/14.4/14.7 未核实，改这里即可回填）
# 约定：键是本模块的逻辑名，值是 provider 报文里的真实字段名（支持 "a.b.c" 点路径，
# 数组取第一个元素）。业务逻辑只准用 MINIMAX_FIELD_MAP["xxx"]，禁止散落魔法字段名。
# 回填方式：拿 scripts/verify_provider.py 的真实回执逐项核对，只改右值，不改代码。
# ══════════════════════════════════════════════════════════════════════════════
MINIMAX_FIELD_MAP: dict[str, str] = {
    # ── 请求侧字段 / 查询参数 ────────────────────────────────
    "req_model": "model",
    "req_prompt": "prompt",
    # §14.1 未核实：图生视频的参考图是传 URL 还是 base64、尺寸/比例/大小限制
    "req_image": "first_frame_image",
    "req_duration": "duration",
    "req_resolution": "resolution",
    "req_aspect": "aspect_ratio",
    "req_seed": "seed",
    "req_audio": "with_audio",
    "req_group_id": "GroupId",
    # ── 提交回执：异步任务句柄（§14.2 未核实字段名与类型）────
    "task_id": "task_id",
    "task_token": "task_token",
    # ── 查询回执：状态字段与终态取值集合（§14.2 未核实）─────
    "status": "status",
    "done_values": "Success,success,DONE,done",
    "failed_values": "Fail,failed,Failed,FAIL,Error,error",
    "pending_values": "Queueing,Processing,Preparing,queueing,processing,preparing",
    # ── 结果文件地址与二次下载接口（§14.3 未核实：地址有效期/是否必须二次下载）──
    "file_url": "file.url",
    "file_id": "file_id",
    "file_retrieve_param": "file_id",
    "file_retrieve_url": "file.download_url",
    # ── 失败原因与计费回执（§14.4 未核实：失败/超时是否计费、回执是否给消耗）──
    "error_message": "base_resp.status_msg",
    "cost_cny": "cost_cny",
    # ── 限流返回码（§14.7 未核实：并发上限与限流返回码）──────
    "rate_limit_status_codes": "429,1002,1004",
}

# AMBIGUITY(§11)：配置表只给了 POLL_TIMEOUT_SEC / DOWNLOAD_TIMEOUT_SEC，没有 submit/query 的单次 HTTP 超时键；
# 此处用模块常量兜底（60s 总超时、10s 连接超时），需要可配置时新增键并改这里。
PROVIDER_HTTP_TIMEOUT_SEC: float = 60.0
PROVIDER_CONNECT_TIMEOUT_SEC: float = 10.0

# 未配置时的统一指引（不得在消息里出现任何 key 值）
_NOT_CONFIGURED_HINT = (
    "MiniMax 客户端未配置：请到 .env 填 MINIMAX_API_KEY（真实的 key，不要提交进仓库），"
    "并确认 MINIMAX_BASE_URL / MINIMAX_GROUP_ID 与 VIDEO_PROVIDER 一致；"
    "没有 key 时请用 VIDEO_PROVIDER=fake 或 --dry-run 走 FakeVideoClient。"
    "§14 未核实前不得放开全量真跑。"
)


class ProviderNotConfigured(RuntimeError):
    """provider 缺少必要配置（典型：MINIMAX_API_KEY 为空）。消息必须能指导用户去 .env 补键。"""


class ProviderError(RuntimeError):
    """provider 调用失败（网络异常、非 200、回执不可解析、缺少关键字段）。消息必须已脱敏。

    `retryable=True` 表示传输层/限流/5xx 之类的瞬时故障：调用方应保留句柄继续轮询（§3.1），
    不得当成"抽卡失败"而重抽（重抽是花钱动作）。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        """构造异常；默认 retryable=False（配置/协议错误，重试无意义）。无副作用。"""
        super().__init__(message)
        self.retryable = retryable


class VideoClient(Protocol):
    """视频 provider 协议（§10.4）。实现方：MiniMaxVideoClient、FakeVideoClient。"""

    name: str

    async def submit(self, req: VideoRequest) -> str:
        """提交一次生成并返回 provider 任务句柄（不得阻塞等待成片）。副作用：网络请求。"""
        ...

    async def poll(self, provider_task_id: str) -> VideoOutcome:
        """查询一次任务状态。

        `.done=False, .failed=False` 表示「仍在生成」（pending），调用方应按 POLL_BACKOFF 继续轮询；
        `.done=True` 时 `.url` 为结果文件地址；`.failed=True` 时 `.error` 说明原因。
        `.cost_cny` 以 provider 回执优先，缺失时回退预估。副作用：网络请求。
        """
        ...


# ── 通用小工具（image.py 复用）───────────────────────────────
def safe_host(url: str) -> str:
    """只取 URL 的 host（含端口），去掉 scheme/路径/查询串/凭据，用于脱敏日志。无副作用。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or ""
    if not host:
        return "<no-host>"
    return f"{host}:{parts.port}" if parts.port else host


def to_float(value: Any, default: float = 0.0) -> float:
    """把回执里的数值（str/int/float/Decimal/None）转 float，非法或非有限值返回 default。无副作用。"""
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def dig_field(payload: Any, path: str) -> Any:
    """按 `"a.b.c"` 点路径取值；遇到 list 取第一个元素；任一层缺失/类型不符返回 None。无副作用。"""
    if not path:
        return None
    current = payload
    for segment in path.split("."):
        if isinstance(current, list):
            current = current[0] if current else None
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return None
    return current


def field(key: str) -> str:
    """读取字段校验表里的字段名；键不存在时抛 ProviderError（防止拼错后静默取空）。无副作用。"""
    try:
        return MINIMAX_FIELD_MAP[key]
    except KeyError as exc:  # 仅开发期会触发
        raise ProviderError(f"MINIMAX_FIELD_MAP 缺少键 {key!r}（内部错误，请补字段映射）") from exc


def value_set(key: str) -> tuple[str, ...]:
    """把字段表里逗号分隔的取值集合解析成元组（用于终态判定）。无副作用。"""
    raw = field(key)
    return tuple(item.strip() for item in raw.replace("，", ",").split(",") if item.strip())


def rate_limit_codes() -> frozenset[int]:
    """限流返回码集合（§14.7 待核实，来自 MINIMAX_FIELD_MAP）。无副作用。"""
    codes: set[int] = set()
    for raw in value_set("rate_limit_status_codes"):
        try:
            codes.add(int(raw))
        except ValueError:
            continue
    return frozenset(codes)


def _request_timeout() -> httpx.Timeout:
    """provider HTTP 超时（连接/读/写/池均必设）。无副作用。"""
    return httpx.Timeout(
        PROVIDER_HTTP_TIMEOUT_SEC,
        connect=PROVIDER_CONNECT_TIMEOUT_SEC,
    )


async def download_bytes(url: str, settings: Settings) -> bytes:
    """用 httpx 下载结果文件（图片与视频共用），超时 DOWNLOAD_TIMEOUT_SEC，非 200 抛 ProviderError。副作用：网络请求。

    §14.3 未核实项：结果地址可能有有效期；调用方应在拿到地址后立即下载。本函数只做一次 GET，不重试（重试由上层按 attempt 处理）。
    """
    if not url:
        raise ProviderError("下载失败：结果地址为空（§14.3 结果文件地址待核实）。")
    timeout_sec = to_float(getattr(settings, "download_timeout_sec", 0.0), 0.0) or 120.0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec)) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"下载异常：host={safe_host(url)} cause={type(exc).__name__}"
        ) from exc
    if response.status_code != 200:
        raise ProviderError(
            f"下载非 200：host={safe_host(url)} status={response.status_code}"
        )
    data = response.content
    if not data:
        raise ProviderError(f"下载得到空文件：host={safe_host(url)}")
    logger.info("下载完成 host={} bytes={}", safe_host(url), len(data))
    return data


class MiniMaxVideoClient:
    """MiniMax 图生视频客户端（§10.4）：submit 建任务、poll 查状态、必要时二次下载取文件地址。

    未配置 key 时构造即抛 ProviderNotConfigured，绝不发启动期网络请求。
    """

    name: str = "minimax-video"

    def __init__(self, settings: Settings) -> None:
        """按 MINIMAX_* 配置构造（不发网络请求）。副作用：无（只在内存保存配置副本）。"""
        self._settings = settings
        api_key = str(getattr(settings, "minimax_api_key", "") or "").strip()
        if not api_key:
            raise ProviderNotConfigured(_NOT_CONFIGURED_HINT)
        base_url = str(getattr(settings, "minimax_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            raise ProviderNotConfigured(
                "MINIMAX_BASE_URL 未配置：请在 .env 填 MiniMax 域名（如 https://api.minimax.chat）。"
            )
        self._api_key = api_key
        self._base_url = base_url
        self._submit_path = str(settings.minimax_video_submit_path or "")
        self._query_path = str(settings.minimax_video_query_path or "")
        self._retrieve_path = str(settings.minimax_file_retrieve_path or "")
        self._group_id = str(getattr(settings, "minimax_group_id", "") or "").strip()
        for path_name, path_value in (
            ("MINIMAX_VIDEO_SUBMIT_PATH", self._submit_path),
            ("MINIMAX_VIDEO_QUERY_PATH", self._query_path),
        ):
            if not path_value:
                raise ProviderNotConfigured(
                    f"{path_name} 未配置：请在 .env 补该路径（§14.2 待核实真实路径）。"
                )
        # 句柄 → 请求（仅内存）：用于回执缺 cost_cny 时按 §10.1 口径估算本次提交成本。
        # 进程重启后为空，此时只信 provider 回执，不臆造费用。
        self._submitted: dict[str, VideoRequest] = {}
        logger.info(
            "MiniMaxVideoClient 就绪 host={} submit={} query={} group_id={}",
            safe_host(base_url),
            self._submit_path,
            self._query_path,
            "set" if self._group_id else "empty",
        )

    # ── 协议方法 ────────────────────────────────────────────
    async def submit(self, req: VideoRequest) -> str:
        """提交图生视频任务并返回任务句柄；回执缺句柄字段时抛 ProviderError。副作用：网络请求、内存登记。"""
        payload = self._submit_payload(req)
        data = await self._request(
            "POST", self._url(self._submit_path), json_body=payload
        )
        task_id = dig_field(data, field("task_id"))
        if not isinstance(task_id, str) or not task_id.strip():
            raise ProviderError(
                "提交回执缺少任务句柄字段 "
                f"{field('task_id')!r}（§14.2 未核实，改 MINIMAX_FIELD_MAP 回填）；"
                f"回执顶层键={sorted(data) if isinstance(data, dict) else type(data).__name__}"
            )
        task_id = task_id.strip()
        self._submitted[task_id] = req
        logger.info(
            "视频提交成功 shot={} tier={} handle={} duration={}s",
            req.shot_id or "-",
            req.tier,
            task_id,
            req.duration_sec,
        )
        return task_id

    async def poll(self, provider_task_id: str) -> VideoOutcome:
        """查询一次任务；回执字段缺失/类型异常转 failed 回执，传输层瞬时故障转 pending 并保留句柄（§3.1）。

        副作用：网络请求、可能触发一次二次下载查询；不修改任何持久状态。
        """
        handle = (provider_task_id or "").strip()
        if not handle:
            return VideoOutcome(failed=True, error="任务句柄为空，无法轮询。")
        try:
            data = await self._request(
                "GET",
                self._url(self._query_path),
                params={field("task_id"): handle},
            )
        except ProviderError as exc:
            if exc.retryable:
                # §3.1：MiniMax 短时不可用时保留句柄、保留 PENDING，由 POLL_TIMEOUT_SEC 兜底，
                # 绝不因为一次网络抖动就重抽（重抽即重复计费）。
                logger.warning("轮询瞬时故障，保留句柄继续轮询：handle={} cause={}", handle, exc)
                return VideoOutcome(
                    done=False,
                    failed=False,
                    error=str(exc),
                    provider_task_id=handle,
                    status="transport-retry",
                )
            return VideoOutcome(
                failed=True,
                error=str(exc),
                provider_task_id=handle,
                status="provider-error",
            )
        return await self._to_outcome(handle, data)

    # ── 内部实现 ────────────────────────────────────────────
    async def _to_outcome(self, handle: str, data: Any) -> VideoOutcome:
        """把查询回执规整成三态 VideoOutcome（防御式，缺字段不抛栈）。副作用：可能发一次文件查询请求。"""
        raw = data if isinstance(data, dict) else {"payload": data}
        status_value = dig_field(data, field("status"))
        status = status_value.strip() if isinstance(status_value, str) else ""
        reported_cost = to_float(dig_field(data, field("cost_cny")), 0.0)
        estimate = self._estimate_cost(handle)
        error_message = dig_field(data, field("error_message"))
        error_text = error_message.strip() if isinstance(error_message, str) else ""

        if not status:
            # 字段缺失或类型异常（§14.2 未核实）：按需求转成 failed 回执，不让 pump 拿到假 pending。
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            return VideoOutcome(
                failed=True,
                error=(
                    f"轮询回执缺少状态字段 {field('status')!r} 或类型异常（§14.2 未核实，"
                    f"改 MINIMAX_FIELD_MAP 回填）；回执顶层键={keys}"
                ),
                provider_task_id=handle,
                status="unknown-status",
                raw=raw,
            )

        if status in value_set("failed_values"):
            # §14.4 未核实失败是否计费：只认 provider 明确回执的金额，没有就记 0，不臆测。
            return VideoOutcome(
                failed=True,
                cost_cny=round(reported_cost, 3) if reported_cost > 0 else 0.0,
                error=error_text or f"provider 返回失败终态 status={status!r}",
                provider_task_id=handle,
                status=status,
                raw=raw,
            )

        if status not in value_set("done_values"):
            # 状态字段存在且不是终态：仍在生成（pending），cost 记 0，仅回显状态；
            # 调用方按 POLL_BACKOFF 继续轮询，超 POLL_TIMEOUT_SEC 后置回 PENDING 且保留句柄（§3.1）。
            # 未知取值也走这条保守分支（不臆断成功/失败），终态集合待 §14.2 核实后回填。
            return VideoOutcome(
                done=False,
                failed=False,
                cost_cny=0.0,
                provider_task_id=handle,
                status=status,
                raw=raw,
            )

        url = await self._resolve_url(data)
        if not url:
            return VideoOutcome(
                done=False,
                failed=True,
                cost_cny=pricing.provider_reported_cost(reported_cost, estimate),
                error=(
                    "任务已到终态但取不到结果文件地址（§14.3 未核实：file_url / 二次下载接口字段名）；"
                    "请用 scripts/verify_provider.py 实测后回填 MINIMAX_FIELD_MAP。"
                ),
                provider_task_id=handle,
                status=status,
                raw=raw,
            )

        return VideoOutcome(
            done=True,
            failed=False,
            cost_cny=pricing.provider_reported_cost(reported_cost, estimate),
            url=url,
            provider_task_id=handle,
            status=status,
            raw=raw,
        )

    async def _resolve_url(self, data: Any) -> str | None:
        """优先取回执里的直链；没有则用 file_id 走二次下载接口（§14.3）。副作用：可能发一次请求。"""
        direct = dig_field(data, field("file_url"))
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        file_id = dig_field(data, field("file_id"))
        if not isinstance(file_id, str) or not file_id.strip() or not self._retrieve_path:
            return None
        try:
            info = await self._request(
                "GET",
                self._url(self._retrieve_path),
                params={field("file_retrieve_param"): file_id.strip()},
            )
        except ProviderError as exc:
            logger.warning("二次下载接口失败 file_id={} cause={}", file_id, exc)
            return None
        candidate = dig_field(info, field("file_retrieve_url"))
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        logger.warning(
            "二次下载回执缺少地址字段 {}（§14.3 待核实）file_id={}",
            field("file_retrieve_url"),
            file_id,
        )
        return None

    def _estimate_cost(self, handle: str) -> float:
        """按 §10.1 口径估算本次提交成本（单次提交 = 1 次抽卡）；未知句柄返回 0。无副作用。"""
        req = self._submitted.get(handle)
        if req is None:
            return 0.0
        try:
            return pricing.est_video_cost(
                req.tier, req.duration_sec, self._settings, expected_attempts=1.0
            )
        except pricing.PricingError as exc:
            logger.warning("视频成本预估不可用（回执也无 cost_cny）：{}", exc)
            return 0.0

    def _submit_payload(self, req: VideoRequest) -> dict[str, Any]:
        """组装提交报文（字段名全部经 MINIMAX_FIELD_MAP）。无副作用。"""
        duration = float(req.duration_sec)
        payload: dict[str, Any] = {
            field("req_model"): req.model or self._settings.video_model(req.tier),
            field("req_prompt"): req.motion_prompt,
            field("req_duration"): int(duration) if duration.is_integer() else duration,
            field("req_resolution"): req.resolution,
            field("req_aspect"): req.aspect or self._settings.video_aspect,
        }
        if req.image_ref:
            # §14.1 待核实：当前按"传 URL/路径字符串"实现；若实测要 base64 或先传素材换 file_id，
            # 只需改这里的取值方式与 req_image 字段名。
            payload[field("req_image")] = req.image_ref
        if req.seed is not None:
            payload[field("req_seed")] = int(req.seed)
        if req.generate_audio:
            payload[field("req_audio")] = True
        return payload

    def _url(self, path: str) -> str:
        """拼 URL（只此一处；路径来自配置）。无副作用。"""
        if not path:
            raise ProviderError("provider 路径未配置（内部错误）。")
        return f"{self._base_url}{path if path.startswith('/') else '/' + path}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """发一次带鉴权头的请求并返回 JSON；失败一律抛脱敏的 ProviderError。副作用：网络请求。

        鉴权头只在内存里拼接，日志绝不打 key 与完整 URL（只打 host）。
        网络异常 / 限流 / 5xx 标 retryable=True（保留句柄重试），4xx/协议错误标 False（快速失败）。
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        query: dict[str, Any] = dict(params or {})
        if self._group_id:
            query.setdefault(field("req_group_id"), self._group_id)
        try:
            async with httpx.AsyncClient(timeout=_request_timeout()) as client:
                response = await client.request(
                    method, url, headers=headers, params=query, json=json_body
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{method} 请求异常：host={safe_host(url)} cause={type(exc).__name__}",
                retryable=True,
            ) from exc

        if response.status_code in rate_limit_codes():
            raise ProviderError(
                f"限流：host={safe_host(url)} status={response.status_code}"
                "（§14.7 待核实限流返回码与并发上限，先降 ASSET_CONCURRENCY 再重试）",
                retryable=True,
            )
        if response.status_code != 200:
            raise ProviderError(
                f"{method} 非 200：host={safe_host(url)} status={response.status_code}",
                retryable=response.status_code >= 500,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                f"回执不是合法 JSON：host={safe_host(url)}"
            ) from exc


def get_video_client(settings: Settings, dry_run: bool = False) -> VideoClient:
    """按 VIDEO_PROVIDER / dry_run 选择视频客户端（§10.4）。副作用：构造 MiniMax 客户端时只读配置，不发网络。

    - `dry_run=True`（或 Settings.dry_run / VIDEO_PROVIDER=fake）→ FakeVideoClient（零花费）。
    - `minimax` → MiniMaxVideoClient；缺 MINIMAX_API_KEY 时抛 ProviderNotConfigured（消息指引去 .env 补键）。
    - 其他取值 → ProviderNotConfigured（一期只有 minimax / fake 两个实现）。
    """
    provider = str(getattr(settings, "video_provider", "minimax") or "minimax").strip().lower()
    if dry_run or bool(getattr(settings, "dry_run", False)) or provider == "fake":
        logger.info("视频 provider 选择 FakeVideoClient（dry_run={} provider={}）", dry_run, provider)
        return FakeVideoClient()
    if provider == "minimax":
        return MiniMaxVideoClient(settings)
    raise ProviderNotConfigured(
        f"VIDEO_PROVIDER={provider!r} 不是已知实现：一期只支持 minimax / fake（§10.4）。"
        "请改 .env 的 VIDEO_PROVIDER，或用 --dry-run 走 FakeVideoClient。"
    )
