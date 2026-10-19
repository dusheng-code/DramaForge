"""职责：图像 provider 抽象与 MiniMax 图像客户端（§10.4）：定妆图与首帧共用一次同步生成调用，字段名收口成 MINIMAX_IMAGE_FIELD_MAP。
输入：ImageRequest、MiniMax 回执 JSON、Settings 的 MINIMAX_* / IMAGE_*。
输出：ImageResult（优先 b64、其次 url；failed 时带 error）；get_image_client 按 IMAGE_PROVIDER 选实现。

副作用：MiniMaxImageClient.generate 发 HTTPS 请求；不写磁盘（落盘由 assets/pump 经 io/artifacts 完成）。
禁止：启动期发网络请求；日志打印 API Key 或带凭据的完整 URL（只打 host）。
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from loguru import logger

from ..contracts.assets import ImageRequest, ImageResult
from ..core.config import Settings
from . import pricing
from .fake_image import FakeImageClient
from .video import (
    PROVIDER_CONNECT_TIMEOUT_SEC,
    PROVIDER_HTTP_TIMEOUT_SEC,
    ProviderError,
    ProviderNotConfigured,
    dig_field,
    rate_limit_codes,
    safe_host,
    to_float,
)

# ══════════════════════════════════════════════════════════════════════════════
# MiniMax 图像字段名总表（§14.1/14.4/14.7 未核实，改这里即可回填）
# 约定：键是本模块的逻辑名，值是 provider 报文里的真实字段名（支持 "a.b.c" 点路径，数组取第一个元素）。
# 业务逻辑只准用 MINIMAX_IMAGE_FIELD_MAP["xxx"]，禁止散落魔法字段名。
# ══════════════════════════════════════════════════════════════════════════════
MINIMAX_IMAGE_FIELD_MAP: dict[str, str] = {
    # ── 请求侧 ──────────────────────────────────────────────
    "req_model": "model",
    "req_prompt": "prompt",
    # §14.1 未核实：参考图（定妆图/首帧参考）是 URL 还是 base64，以及尺寸/比例限制
    "req_ref_image": "subject_reference",
    "req_resolution": "resolution",
    "req_aspect": "aspect_ratio",
    "req_seed": "seed",
    "req_response_format": "response_format",
    "req_group_id": "GroupId",
    # ── 回执侧：优先 b64，其次 url ───────────────────────────
    "b64": "data.image_base64",
    "url": "data.image_urls",
    "status_code": "base_resp.status_code",
    "status_msg": "base_resp.status_msg",
    "cost_cny": "cost_cny",
    # ── 限流返回码（§14.7 待核实）───────────────────────────
    "rate_limit_status_codes": "429,1002,1004",
}

# 回执格式偏好：base64 省一次下载（pump 直接落盘），§14.3 结果地址有效期问题也一并规避。
_IMAGE_RESPONSE_FORMAT: str = "base64"

# 未配置时的统一指引（不得在消息里出现任何 key 值）
_NOT_CONFIGURED_HINT = (
    "MiniMax 图像客户端未配置：请到 .env 填 MINIMAX_API_KEY（真实 key，不要提交进仓库），"
    "并确认 MINIMAX_BASE_URL / MINIMAX_GROUP_ID；"
    "没有 key 时请用 IMAGE_PROVIDER=fake 或 --dry-run 走 FakeImageClient。"
)


class ImageClient(Protocol):
    """图像 provider 协议（§10.4）。实现方：MiniMaxImageClient、FakeImageClient。"""

    name: str

    async def generate(self, req: ImageRequest) -> ImageResult:
        """生成一张图（定妆图或首帧），同步等待结果。

        `.failed=True` 时 `.error` 说明原因；成功时 `.b64` 优先（内联），否则 `.url` 交给 pump 下载。
        副作用：网络请求。
        """
        ...


def image_field(key: str) -> str:
    """读取图像字段表里的字段名；键不存在抛 ProviderError（防止拼错后静默取空）。无副作用。"""
    try:
        return MINIMAX_IMAGE_FIELD_MAP[key]
    except KeyError as exc:  # 仅开发期会触发
        raise ProviderError(f"MINIMAX_IMAGE_FIELD_MAP 缺少键 {key!r}（内部错误，请补字段映射）") from exc


def image_value_set(key: str) -> tuple[str, ...]:
    """把图像字段表里逗号分隔的取值集合解析成元组。无副作用。"""
    raw = image_field(key)
    return tuple(item.strip() for item in raw.replace("，", ",").split(",") if item.strip())


def _image_rate_limit_codes() -> frozenset[int]:
    """图像接口的限流码（§14.7 待核实；缺配置时回退视频侧同口径）。无副作用。"""
    codes: set[int] = set()
    for raw in image_value_set("rate_limit_status_codes"):
        try:
            codes.add(int(raw))
        except ValueError:
            continue
    return frozenset(codes) or rate_limit_codes()


class MiniMaxImageClient:
    """MiniMax 图像客户端（§10.4）：一次请求拿 b64/url，不轮询。未配置 key 时构造即抛 ProviderNotConfigured。"""

    name: str = "minimax-image"

    def __init__(self, settings: Settings) -> None:
        """按 MINIMAX_* / IMAGE_* 配置构造（不发网络请求）。无副作用。"""
        self._settings = settings
        api_key = str(getattr(settings, "minimax_api_key", "") or "").strip()
        if not api_key:
            raise ProviderNotConfigured(_NOT_CONFIGURED_HINT)
        base_url = str(getattr(settings, "minimax_base_url", "") or "").strip().rstrip("/")
        if not base_url:
            raise ProviderNotConfigured(
                "MINIMAX_BASE_URL 未配置：请在 .env 填 MiniMax 域名（如 https://api.minimax.chat）。"
            )
        self._image_path = str(getattr(settings, "minimax_image_path", "") or "")
        if not self._image_path:
            raise ProviderNotConfigured(
                "MINIMAX_IMAGE_PATH 未配置：请在 .env 补该路径（§14 待核实真实路径）。"
            )
        self._api_key = api_key
        self._base_url = base_url
        self._group_id = str(getattr(settings, "minimax_group_id", "") or "").strip()
        logger.info(
            "MiniMaxImageClient 就绪 host={} path={} group_id={}",
            safe_host(base_url),
            self._image_path,
            "set" if self._group_id else "empty",
        )

    async def generate(self, req: ImageRequest) -> ImageResult:
        """发一次图像生成请求并规整回执：优先 b64、其次 url；异常一律转 failed 回执。副作用：网络请求。"""
        payload = self._payload(req)
        try:
            data = await self._post(payload)
        except ProviderError as exc:
            return ImageResult(done=False, failed=True, error=str(exc), raw={})

        raw = data if isinstance(data, dict) else {"payload": data}
        status_code = dig_field(data, image_field("status_code"))
        error_text = dig_field(data, image_field("status_msg"))
        if status_code is not None and to_float(status_code, 0.0) != 0.0:
            return ImageResult(
                done=False,
                failed=True,
                error=str(error_text or f"provider 返回 status_code={status_code!r}"),
                raw=raw,
            )

        b64 = dig_field(data, image_field("b64"))
        url = dig_field(data, image_field("url"))
        b64_text = b64.strip() if isinstance(b64, str) and b64.strip() else None
        url_text = url.strip() if isinstance(url, str) and url.strip() else None
        if not b64_text and not url_text:
            return ImageResult(
                done=False,
                failed=True,
                error=(
                    "图像回执既无 b64 也无 url（§14.1/§14.3 未核实字段名 "
                    f"{image_field('b64')!r} / {image_field('url')!r}）；"
                    "请用 scripts/verify_provider.py 实测后回填 MINIMAX_IMAGE_FIELD_MAP。"
                ),
                raw=raw,
            )

        reported_cost = to_float(dig_field(data, image_field("cost_cny")), 0.0)
        cost = pricing.provider_reported_cost(reported_cost, self._estimate_cost())
        logger.info(
            "图像生成成功 kind={} shot={} via={} cost={}",
            req.kind,
            req.shot_id or "-",
            "b64" if b64_text else "url",
            cost,
        )
        return ImageResult(
            done=True,
            failed=False,
            b64=b64_text,
            url=None if b64_text else url_text,
            cost_cny=cost,
            raw=raw,
        )

    def _estimate_cost(self) -> float:
        """按 IMAGE_PRICE_CNY 估算单张成本；缺配置时返回 0（图像链路可继续，报价侧会单独报错）。无副作用。"""
        try:
            return pricing.image_unit_price(self._settings)
        except pricing.PricingError as exc:
            logger.warning("图像成本预估不可用（回执也无 cost_cny）：{}", exc)
            return 0.0

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        """组装图像请求报文（字段名全部经 MINIMAX_IMAGE_FIELD_MAP）。无副作用。"""
        payload: dict[str, Any] = {
            image_field("req_model"): req.model or self._settings.image_model,
            image_field("req_prompt"): req.prompt,
            image_field("req_resolution"): req.resolution or self._settings.image_resolution,
            image_field("req_aspect"): req.aspect or self._settings.image_aspect,
            image_field("req_response_format"): _IMAGE_RESPONSE_FORMAT,
        }
        if req.ref_image:
            # §14.1 待核实：当前按"传 URL/路径字符串"实现；若实测要 base64，只改这里取值方式。
            payload[image_field("req_ref_image")] = req.ref_image
        if req.seed is not None:
            payload[image_field("req_seed")] = int(req.seed)
        return payload

    async def _post(self, payload: dict[str, Any]) -> Any:
        """发一次带鉴权的 POST 并返回 JSON；失败抛脱敏 ProviderError。副作用：网络请求。"""
        url = f"{self._base_url}{self._image_path if self._image_path.startswith('/') else '/' + self._image_path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        params: dict[str, Any] = {}
        if self._group_id:
            params[image_field("req_group_id")] = self._group_id
        timeout = httpx.Timeout(
            PROVIDER_HTTP_TIMEOUT_SEC, connect=PROVIDER_CONNECT_TIMEOUT_SEC
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, params=params, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"POST 请求异常：host={safe_host(url)} cause={type(exc).__name__}"
            ) from exc
        if response.status_code in _image_rate_limit_codes():
            raise ProviderError(
                f"限流：host={safe_host(url)} status={response.status_code}"
                "（§14.7 待核实限流返回码与并发上限，先降 IMAGE_CONCURRENCY 再重试）"
            )
        if response.status_code != 200:
            raise ProviderError(
                f"POST 非 200：host={safe_host(url)} status={response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"回执不是合法 JSON：host={safe_host(url)}") from exc


def get_image_client(settings: Settings, dry_run: bool = False) -> ImageClient:
    """按 IMAGE_PROVIDER / dry_run 选择图像客户端（§10.4）。副作用：构造 MiniMax 客户端时只读配置，不发网络。

    - `dry_run=True`（或 Settings.dry_run / IMAGE_PROVIDER=fake）→ FakeImageClient（1×1 PNG、零花费）。
    - `minimax` → MiniMaxImageClient；缺 MINIMAX_API_KEY 时抛 ProviderNotConfigured。
    - 其他取值 → ProviderNotConfigured。
    """
    provider = str(getattr(settings, "image_provider", "minimax") or "minimax").strip().lower()
    if dry_run or bool(getattr(settings, "dry_run", False)) or provider == "fake":
        logger.info("图像 provider 选择 FakeImageClient（dry_run={} provider={}）", dry_run, provider)
        return FakeImageClient()
    if provider == "minimax":
        return MiniMaxImageClient(settings)
    raise ProviderNotConfigured(
        f"IMAGE_PROVIDER={provider!r} 不是已知实现：一期只支持 minimax / fake（§10.4）。"
        "请改 .env 的 IMAGE_PROVIDER，或用 --dry-run 走 FakeImageClient。"
    )
