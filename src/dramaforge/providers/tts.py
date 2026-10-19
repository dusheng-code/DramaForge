"""职责：TTS 的最薄封装（§1「不做音色训练/口型对齐」）：文本 → 音频字节，字段名收口成 MINIMAX_TTS_FIELD_MAP，另提供 FakeTTSClient。
输入：文本、可选音色；Settings 的 MINIMAX_*（TTS 路径/模型在 §11 缺失，见下方 AMBIGUITY）。
输出：音频字节（MiniMax 返回 mp3，Fake 返回固定 WAV 头字节）。

副作用：MiniMaxTTSClient.synthesize 发 HTTPS 请求；不写磁盘（落盘由调用方经 io/artifacts 完成）。
禁止：接进主链路——主链路只产出 tts_text（§1 不做合成），本模块仅供离线试音与后续里程碑使用。
"""

from __future__ import annotations

import base64
import struct
from typing import Any, Protocol

import httpx
from loguru import logger

from ..core.config import Settings
from .video import (
    PROVIDER_CONNECT_TIMEOUT_SEC,
    PROVIDER_HTTP_TIMEOUT_SEC,
    ProviderError,
    ProviderNotConfigured,
    dig_field,
    rate_limit_codes,
    safe_host,
)

# ══════════════════════════════════════════════════════════════════════════════
# MiniMax TTS 字段名总表（§14 未列 TTS，全部待实测，改这里即可回填）
# 约定：键是本模块的逻辑名，值是 provider 报文里的真实字段名（支持 "a.b.c" 点路径，数组取第一个元素）。
# ══════════════════════════════════════════════════════════════════════════════
MINIMAX_TTS_FIELD_MAP: dict[str, str] = {
    # ── 请求侧 ──────────────────────────────────────────────
    "req_model": "model",
    "req_text": "text",
    "req_voice": "voice_setting.voice_id",
    "req_group_id": "GroupId",
    # ── 回执侧 ──────────────────────────────────────────────
    "audio_hex": "data.audio",
    "audio_url": "data.audio_url",
    "status_code": "base_resp.status_code",
    "status_msg": "base_resp.status_msg",
    "rate_limit_status_codes": "429,1002,1004",
}

# AMBIGUITY(§11)：配置表没有 TTS 专用键（MINIMAX_TTS_PATH / MINIMAX_TTS_MODEL / MINIMAX_TTS_VOICE）。
# 本模块用常量承载默认值，并在构造时用 getattr 允许 Settings 覆盖（新增键时无需改本文件逻辑）。
DEFAULT_TTS_PATH: str = "/v1/t2a_v2"
DEFAULT_TTS_MODEL: str = "speech-02-hd"
DEFAULT_TTS_VOICE: str = "female-shaonv"

# 一份结构合法的 44 字节 WAV 头（RIFF/WAVE/fmt/data，单声道 16bit 24kHz，0 个采样），
# 供 Fake 路径产出可被播放器识别的最小文件；不参与任何真实音频链路。
WAV_HEADER: bytes = (
    b"RIFF"
    + struct.pack("<I", 36)
    + b"WAVEfmt "
    + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
    + b"data"
    + struct.pack("<I", 0)
)


class TTSClient(Protocol):
    """TTS provider 协议：只做文本→音频字节，不做音色训练/口型对齐（§1）。"""

    name: str

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """把一段 tts_text 合成音频并返回字节；失败抛 ProviderError。副作用：网络请求。"""
        ...


def tts_field(key: str) -> str:
    """读取 TTS 字段表里的字段名；键不存在抛 ProviderError（防止拼错后静默取空）。无副作用。"""
    try:
        return MINIMAX_TTS_FIELD_MAP[key]
    except KeyError as exc:  # 仅开发期会触发
        raise ProviderError(f"MINIMAX_TTS_FIELD_MAP 缺少键 {key!r}（内部错误，请补字段映射）") from exc


def _tts_rate_limit_codes() -> frozenset[int]:
    """TTS 接口限流码（待实测；缺配置回退视频侧同口径）。无副作用。"""
    codes: set[int] = set()
    raw = tts_field("rate_limit_status_codes")
    for item in raw.replace("，", ",").split(","):
        try:
            codes.add(int(item.strip()))
        except ValueError:
            continue
    return frozenset(codes) or rate_limit_codes()


class MiniMaxTTSClient:
    """MiniMax TTS 最薄封装：一次 POST 拿 audio（hex）或 audio_url。未配置 key 时构造即抛 ProviderNotConfigured。

    不接主链路：主链路只产 tts_text（§1）。
    """

    name: str = "minimax-tts"

    def __init__(self, settings: Settings) -> None:
        """按 MINIMAX_*（TTS 键用 getattr 兜底）构造（不发网络请求）。无副作用。"""
        self._settings = settings
        api_key = str(getattr(settings, "minimax_api_key", "") or "").strip()
        if not api_key:
            raise ProviderNotConfigured(
                "MiniMax TTS 客户端未配置：请到 .env 填 MINIMAX_API_KEY（真实 key，不要提交进仓库）；"
                "没有 key 时请用 FakeTTSClient 做离线试音。TTS 不接主链路（§1）。"
            )
        self._api_key = api_key
        self._base_url = str(getattr(settings, "minimax_base_url", "") or "").strip().rstrip("/")
        if not self._base_url:
            raise ProviderNotConfigured(
                "MINIMAX_BASE_URL 未配置：请在 .env 填 MiniMax 域名（如 https://api.minimax.chat）。"
            )
        self._path = str(getattr(settings, "minimax_tts_path", "") or DEFAULT_TTS_PATH)
        self._model = str(getattr(settings, "minimax_tts_model", "") or DEFAULT_TTS_MODEL)
        self._voice = str(getattr(settings, "minimax_tts_voice", "") or DEFAULT_TTS_VOICE)
        self._group_id = str(getattr(settings, "minimax_group_id", "") or "").strip()
        logger.info(
            "MiniMaxTTSClient 就绪 host={} path={} voice={}（不接主链路）",
            safe_host(self._base_url),
            self._path,
            self._voice,
        )

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """把文本合成为音频字节：优先回执里的 hex 音频，其次 audio_url 下载；失败抛 ProviderError。副作用：网络请求。"""
        if not text or not text.strip():
            raise ProviderError("TTS 文本为空，拒绝请求。")
        payload: dict[str, Any] = {
            tts_field("req_model"): self._model,
            tts_field("req_text"): text,
            tts_field("req_voice"): voice or self._voice,
        }
        data = await self._post(payload)

        status_code = dig_field(data, tts_field("status_code"))
        if status_code is not None and str(status_code).strip() not in ("0", "0.0"):
            message = dig_field(data, tts_field("status_msg"))
            raise ProviderError(
                f"TTS 回执失败 status_code={status_code!r} msg={message!r}"
            )

        audio_hex = dig_field(data, tts_field("audio_hex"))
        if isinstance(audio_hex, str) and audio_hex.strip():
            try:
                audio = bytes.fromhex(audio_hex.strip())
            except ValueError:
                # 部分实现返回 base64 而非 hex：两种都试，字段名未核实前保持宽容。
                try:
                    audio = base64.b64decode(audio_hex.strip(), validate=False)
                except (ValueError, TypeError) as exc:
                    raise ProviderError("TTS 回执音频既非 hex 也非 base64。") from exc
            if audio:
                return audio

        audio_url = dig_field(data, tts_field("audio_url"))
        if isinstance(audio_url, str) and audio_url.strip():
            return await self._download(audio_url.strip())

        raise ProviderError(
            "TTS 回执既无音频字节也无地址（MINIMAX_TTS_FIELD_MAP 待实测回填）。"
        )

    async def _post(self, payload: dict[str, Any]) -> Any:
        """发一次带鉴权的 POST 并返回 JSON；失败抛脱敏 ProviderError。副作用：网络请求。"""
        url = f"{self._base_url}{self._path if self._path.startswith('/') else '/' + self._path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        params: dict[str, Any] = {}
        if self._group_id:
            params[tts_field("req_group_id")] = self._group_id
        timeout = httpx.Timeout(
            PROVIDER_HTTP_TIMEOUT_SEC, connect=PROVIDER_CONNECT_TIMEOUT_SEC
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, params=params, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"TTS 请求异常：host={safe_host(url)} cause={type(exc).__name__}"
            ) from exc
        if response.status_code in _tts_rate_limit_codes():
            raise ProviderError(
                f"TTS 限流：host={safe_host(url)} status={response.status_code}（配额/并发待实测）"
            )
        if response.status_code != 200:
            raise ProviderError(
                f"TTS 非 200：host={safe_host(url)} status={response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"TTS 回执不是合法 JSON：host={safe_host(url)}") from exc

    async def _download(self, url: str) -> bytes:
        """下载 TTS 音频地址（复用统一超时口径）。副作用：网络请求。"""
        timeout_sec = float(getattr(self._settings, "download_timeout_sec", 0.0) or 120.0)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec)) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"TTS 音频下载异常：host={safe_host(url)} cause={type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                f"TTS 音频下载非 200：host={safe_host(url)} status={response.status_code}"
            )
        return response.content


class FakeTTSClient:
    """离线试音用的假 TTS：固定返回 44 字节 WAV 头，不发网络、不写磁盘。"""

    name: str = "fake-tts"

    def __init__(self) -> None:
        """无参数构造。无副作用。"""
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """返回固定 WAV 头字节并记录调用。副作用：内存计数。"""
        self.calls.append((text, voice or DEFAULT_TTS_VOICE))
        logger.debug("FakeTTSClient 合成占位音频 chars={}", len(text))
        return WAV_HEADER


def get_tts_client(settings: Settings, *, dry_run: bool = False) -> TTSClient:
    """按 Settings 构造 TTS 客户端（若未配置 key 或 dry_run 则返回 FakeTTSClient）。无副作用。"""
    if dry_run or not settings.minimax_api_key:
        return FakeTTSClient()
    return MiniMaxTTSClient(settings)

