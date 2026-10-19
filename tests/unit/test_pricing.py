"""职责：providers/pricing.py 的计价口径单测（§10.1/§10.4），外加 FakeVideoClient / FakeImageClient 的脚本化行为单测（M7/M8 断言口径）。
输入：Settings 覆盖（TIER_PRICE / IMAGE_PRICE_CNY）、FakeVideoClient 构造参数、VideoRequest / ImageRequest。
输出：pytest 断言结果。

副作用：无网络请求、无磁盘写入（Fake 客户端全在内存内）。
"""

from __future__ import annotations

import base64
import struct

import pytest

from dramaforge.contracts.assets import ImageRequest, VideoRequest
from dramaforge.core.config import Settings
from dramaforge.providers import pricing
from dramaforge.providers.fake_image import FAKE_PNG_B64, FakeImageClient
from dramaforge.providers.fake_video import FakeVideoClient
from dramaforge.providers.image import get_image_client
from dramaforge.providers.tts import DEFAULT_TTS_VOICE, WAV_HEADER, FakeTTSClient
from dramaforge.providers.video import (
    MiniMaxVideoClient,
    ProviderError,
    ProviderNotConfigured,
    get_video_client,
)


def _settings(**overrides: object) -> Settings:
    """测试用 Settings：强制 TIER_PRICE / IMAGE_PRICE_CNY 为已知值，其余沿用环境。无副作用。"""
    base: dict[str, object] = {
        "tier_price_raw": "fast:0.225,hero:0.400",
        "image_price_cny": 0.2,
    }
    base.update(overrides)
    return Settings().model_copy(update=base)


# ── 单价格式解析 ────────────────────────────────────────────
def test_parse_tier_prices_ok() -> None:
    """标准格式解析成 dict，含中文逗号与空白。无副作用。"""
    assert pricing.parse_tier_prices("fast:0.225,hero:0.400") == {"fast": 0.225, "hero": 0.4}
    assert pricing.parse_tier_prices(" fast:0.225 ， hero:0.400 ") == {
        "fast": 0.225,
        "hero": 0.4,
    }


def test_parse_tier_prices_skips_illegal_items() -> None:
    """无冒号 / 空键 / 非数值 / 负数 / 空串 一律跳过，不抛异常。无副作用。"""
    raw = "fast:0.225,broken,:0.1,hero:abc,neg:-1,hero:0.400,zero:0"
    assert pricing.parse_tier_prices(raw) == {"fast": 0.225, "hero": 0.4, "zero": 0.0}
    assert pricing.parse_tier_prices("") == {}


def test_video_unit_price_reads_tier_price() -> None:
    """video_unit_price 直接读 TIER_PRICE 的档位单价。无副作用。"""
    settings = _settings()
    assert pricing.video_unit_price("fast", settings) == 0.225
    assert pricing.video_unit_price("hero", settings) == 0.400


def test_video_unit_price_missing_tier_raises() -> None:
    """缺档位必须抛 PricingError（不可静默按 0 元报价）。无副作用。"""
    settings = _settings(tier_price_raw="fast:0.225")
    with pytest.raises(pricing.PricingError) as excinfo:
        pricing.video_unit_price("hero", settings)
    assert "hero" in str(excinfo.value)
    assert "TIER_PRICE" in str(excinfo.value)


def test_video_unit_price_zero_price_raises() -> None:
    """档位单价为 0 视为未配置，同样抛 PricingError。无副作用。"""
    settings = _settings(tier_price_raw="fast:0")
    with pytest.raises(pricing.PricingError):
        pricing.video_unit_price("fast", settings)


def test_image_unit_price_reads_config() -> None:
    """image_unit_price 读 IMAGE_PRICE_CNY；非正同样抛错。无副作用。"""
    assert pricing.image_unit_price(_settings()) == 0.2
    with pytest.raises(pricing.PricingError):
        pricing.image_unit_price(_settings(image_price_cny=0.0))


# ── 期望抽卡次数 ────────────────────────────────────────────
def test_expected_attempts_inverse_pass_rate() -> None:
    """期望抽卡次数 = 1 / 通过率（保留 3 位）。无副作用。"""
    assert pricing.expected_attempts(0.75) == 1.333
    assert pricing.expected_attempts(0.5) == 2.0


def test_expected_attempts_clamped() -> None:
    """边界 clamp：≤0 回退默认值，≥1 → 1.0，极低通过率封顶 MAX_ATTEMPT_PER_SHOT。无副作用。"""
    assert pricing.expected_attempts(1.0) == 1.0
    assert pricing.expected_attempts(2.0) == 1.0
    assert pricing.expected_attempts(0.0) == pricing.expected_attempts(pricing.DEFAULT_PASS_RATE)
    assert pricing.expected_attempts(0.1) == float(pricing.MAX_ATTEMPT_PER_SHOT)


# ── 预估成本（手算对照）─────────────────────────────────────
def test_est_video_cost_hand_calc() -> None:
    """手算对照：0.225×6×1=1.35；0.4×10×2=8.0；默认 1 次抽卡；次数 clamp 到 3。无副作用。"""
    settings = _settings()
    assert pricing.est_video_cost("fast", 6, settings) == 1.35
    assert pricing.est_video_cost("hero", 10, settings, expected_attempts=2.0) == 8.0
    # 期望抽卡次数口径：0.225 × 6 × (1/0.75) = 1.8
    assert pricing.est_video_cost("fast", 6, settings, pricing.expected_attempts()) == 1.8
    # clamp：传 10 次也按 MAX_ATTEMPT_PER_SHOT=3 计
    assert pricing.est_video_cost("fast", 6, settings, expected_attempts=10.0) == round(
        0.225 * 6 * 3, 3
    )


def test_est_video_cost_rejects_bad_duration() -> None:
    """时长非正抛 PricingError。无副作用。"""
    with pytest.raises(pricing.PricingError):
        pricing.est_video_cost("fast", 0, _settings())


def test_est_image_costs() -> None:
    """首帧/定妆图成本 = 0.2 × 1.333 = 0.267（3 位小数）。无副作用。"""
    settings = _settings()
    assert pricing.est_keyframe_cost(settings) == 0.267
    assert pricing.est_anchor_cost(settings) == 0.267


# ── provider 回执覆盖 ──────────────────────────────────────
def test_provider_reported_cost_prefers_receipt() -> None:
    """回执 cost_cny > 0 时优先于预估值（§10.4 可被回执覆盖）。无副作用。"""
    assert pricing.provider_reported_cost(0.31, 1.35) == 0.31


def test_provider_reported_cost_falls_back_to_estimate() -> None:
    """回执为 0/缺失/非法时回退预估值。无副作用。"""
    assert pricing.provider_reported_cost(0.0, 1.35) == 1.35
    assert pricing.provider_reported_cost(-1.0, 1.35) == 1.35
    assert pricing.provider_reported_cost(float("nan"), 1.35) == 1.35
    assert pricing.provider_reported_cost(0.0, 0.0) == 0.0


# ── FakeVideoClient 脚本（M7 口径）──────────────────────────
def _video_request() -> VideoRequest:
    """构造一条最小合法 VideoRequest。无副作用。"""
    return VideoRequest(
        shot_id="ep01:sh001",
        image_ref="var/t1/assets/keyframes/ep01_sh001.png",
        motion_prompt="推镜，人物抬头",
        duration_sec=6.0,
        resolution="768p",
        aspect="16:9",
        tier="fast",
    )


async def test_fake_video_submit_then_polls_until_done() -> None:
    """submit → 两次 poll 仍在生成 → 第三次 done：submit_calls=1、poll_calls=3、cost_cny>0。

    M7 断言口径：`提交 1 次` + `每次 poll 都记账`；done 后重复 poll 保持 done 且不重复计费。
    """
    client = FakeVideoClient(done_after_polls=3, cost_cny=0.225)
    req = _video_request()

    handle = await client.submit(req)
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0] is req

    first = await client.poll(handle)
    second = await client.poll(handle)
    third = await client.poll(handle)

    assert (first.done, first.failed, first.cost_cny) == (False, False, 0.0)
    assert (second.done, second.failed, second.cost_cny) == (False, False, 0.0)
    assert (third.done, third.failed) == (True, False)
    assert third.cost_cny > 0
    assert third.url == "fake://video/out.mp4"
    assert len(client.poll_calls) == 3
    assert client.poll_costs == [0.0, 0.0, 0.225]
    assert client.total_cost_cny == 0.225

    fourth = await client.poll(handle)
    assert fourth.done is True and fourth.cost_cny == 0.0


async def test_fake_video_fail_times_and_timeout_forever() -> None:
    """fail_times 让前 N 个句柄首次 poll 失败；timeout_forever 永远 pending 且不计费。无副作用。"""
    failing = FakeVideoClient(fail_times=1, done_after_polls=1, cost_cny=0.1)
    first_handle = await failing.submit(_video_request())
    first_poll = await failing.poll(first_handle)
    assert first_poll.failed is True and first_poll.done is False
    assert "fail_times" in (first_poll.error or "")

    second_handle = await failing.submit(_video_request())
    second_poll = await failing.poll(second_handle)
    assert second_poll.done is True and second_poll.cost_cny == 0.1

    slow = FakeVideoClient(timeout_forever=True, cost_cny=0.1)
    slow_handle = await slow.submit(_video_request())
    for _ in range(5):
        outcome = await slow.poll(slow_handle)
        assert (outcome.done, outcome.failed, outcome.cost_cny) == (False, False, 0.0)
    assert slow.total_cost_cny == 0.0


# ── FakeImageClient（dry-run / M8 之前占位）─────────────────
def test_fake_image_png_is_decodable_1x1() -> None:
    """内联 base64 必须是真实可解码的 1×1 PNG（签名 + IHDR 宽高）。无副作用。"""
    data = base64.b64decode(FAKE_PNG_B64)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1, 1)
    assert len(data) == 69


async def test_fake_image_counts_calls_and_fails_scripted() -> None:
    """calls 记录每次请求；fail_times 期间的调用返回 failed。无副作用。"""
    client = FakeImageClient(fail_times=1, cost_cny=0.2)
    req = ImageRequest(kind="keyframe", shot_id="ep01:sh001", prompt="首帧")
    first = await client.generate(req)
    second = await client.generate(req)
    assert first.failed is True and first.done is False
    assert second.done is True and second.b64 == FAKE_PNG_B64
    assert second.cost_cny == 0.2
    assert len(client.calls) == 2


async def test_fake_tts_returns_valid_wav_header() -> None:
    """FakeTTSClient 返回 44 字节 RIFF/WAVE 头并记录调用（不接主链路，仅离线试音）。无副作用。"""
    client = FakeTTSClient()
    audio = await client.synthesize("你好，这是一句台词。")
    assert len(audio) == 44
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    assert audio == WAV_HEADER
    assert client.name == "fake-tts"
    assert client.calls == [("你好，这是一句台词。", DEFAULT_TTS_VOICE)]


# ── 客户端工厂：未配置必须抛清晰异常（不做任何启动期网络调用）──
def _unconfigured(**overrides: object) -> Settings:
    """无 key / 无 group_id / 非 dry-run 的 Settings，用于验证 ProviderNotConfigured。无副作用。"""
    base: dict[str, object] = {
        "minimax_api_key": "",
        "minimax_group_id": "",
        "dry_run": False,
    }
    base.update(overrides)
    return Settings().model_copy(update=base)


def test_get_video_client_dry_run_is_fake() -> None:
    """dry_run=True 与 VIDEO_PROVIDER=fake 都返回 FakeVideoClient（零花费、不联网）。无副作用。"""
    assert isinstance(get_video_client(_unconfigured(), dry_run=True), FakeVideoClient)
    assert isinstance(get_video_client(_unconfigured(video_provider="fake")), FakeVideoClient)


def test_get_video_client_minimax_without_key_raises() -> None:
    """VIDEO_PROVIDER=minimax 且无 key 时抛 ProviderNotConfigured，消息指路 .env 的 MINIMAX_API_KEY。无副作用。"""
    with pytest.raises(ProviderNotConfigured) as excinfo:
        get_video_client(_unconfigured(video_provider="minimax"))
    assert "MINIMAX_API_KEY" in str(excinfo.value)
    assert ".env" in str(excinfo.value)


def test_get_image_client_fake_and_dry_run() -> None:
    """IMAGE_PROVIDER=fake 与 dry_run=True 都返回 FakeImageClient。无副作用。"""
    assert isinstance(get_image_client(_unconfigured(image_provider="fake")), FakeImageClient)
    assert isinstance(get_image_client(_unconfigured(), dry_run=True), FakeImageClient)


def test_get_image_client_minimax_without_key_raises() -> None:
    """IMAGE_PROVIDER=minimax 且无 key 时抛 ProviderNotConfigured。无副作用。"""
    with pytest.raises(ProviderNotConfigured) as excinfo:
        get_image_client(_unconfigured(image_provider="minimax"))
    assert "MINIMAX_API_KEY" in str(excinfo.value)


def test_unknown_provider_names_raise() -> None:
    """未知 provider 名不得静默降级成 fake（防止真跑被误当演练）。无副作用。"""
    with pytest.raises(ProviderNotConfigured):
        get_video_client(_unconfigured(video_provider="sora"))
    with pytest.raises(ProviderNotConfigured):
        get_image_client(_unconfigured(image_provider="midjourney"))


# ── MiniMax 视频回执的防御式解析（注入假 _request，不联网）──
def _minimax_client() -> MiniMaxVideoClient:
    """构造一个带假 key 的 MiniMaxVideoClient（构造期不联网）。无副作用。"""
    return MiniMaxVideoClient(
        _settings(minimax_api_key="sk-dummy-not-real", minimax_group_id="")
    )


async def test_minimax_poll_done_prefers_receipt_cost() -> None:
    """终态 Success + 直链 → done=True；cost_cny 用 provider 回执（0.31）覆盖预估。无副作用。"""

    async def fake_request(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "status": "Success",
            "file": {"url": "https://files.example/shot.mp4"},
            "cost_cny": 0.31,
        }

    client = _minimax_client()
    client._request = fake_request  # type: ignore[method-assign]
    outcome = await client.poll("handle-1")
    assert (outcome.done, outcome.failed) == (True, False)
    assert outcome.url == "https://files.example/shot.mp4"
    assert outcome.cost_cny == 0.31


async def test_minimax_poll_defensive_on_missing_status() -> None:
    """回执缺状态字段（§14.2 待核实）→ failed 回执 + 可读 error，不抛栈。无副作用。"""

    async def fake_request(*args: object, **kwargs: object) -> dict[str, object]:
        return {"unexpected": True}

    client = _minimax_client()
    client._request = fake_request  # type: ignore[method-assign]
    outcome = await client.poll("handle-1")
    assert outcome.failed is True and outcome.done is False
    assert "status" in (outcome.error or "")


async def test_minimax_poll_transient_error_keeps_handle() -> None:
    """传输层瞬时故障 → pending（保留句柄，不触发重抽）；致命错误 → failed。无副作用。"""

    async def transient(*args: object, **kwargs: object) -> dict[str, object]:
        raise ProviderError("网络抖动", retryable=True)

    async def fatal(*args: object, **kwargs: object) -> dict[str, object]:
        raise ProviderError("401 未授权", retryable=False)

    client = _minimax_client()
    client._request = transient  # type: ignore[method-assign]
    pending = await client.poll("handle-1")
    assert (pending.done, pending.failed) == (False, False)
    assert pending.provider_task_id == "handle-1"
    assert pending.status == "transport-retry"

    client._request = fatal  # type: ignore[method-assign]
    failed = await client.poll("handle-1")
    assert failed.failed is True and failed.done is False
    assert failed.status == "provider-error"
