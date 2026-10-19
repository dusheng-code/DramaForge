"""职责：M8/M9 真实供应商连通性自检与扣费探针脚本。
输入：`--mode [all|video|image|tts]`，`--timeout N`。
输出：供应商 API 延迟、任务提交与轮询状态、计费扣除与回调解析结果。

副作用：产生真实外网 HTTP 请求与 API 扣费（小额探针测试）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from loguru import logger

from ..contracts.assets import ImageRequest, VideoRequest
from ..core.config import Settings, get_settings
from ..providers.image import get_image_client
from ..providers.tts import get_tts_client
from ..providers.video import get_video_client


async def verify_image(settings: Settings, timeout: float) -> bool:
    """测试文生图/定妆图能力。"""
    logger.info("=== 测试图像生成 (MiniMax / 备用通道) ===")
    if not settings.minimax_api_key:
        logger.warning("MINIMAX_API_KEY 未配置，跳过真实图像供应商探针")
        return True

    client = get_image_client(settings, dry_run=False)
    req = ImageRequest(
        prompt="A handsome young Chinese CEO in a dark suit standing in a modern office, cinematic lighting, 8k",
        aspect_ratio="9:16",
        quality_tier="fast",
        dry_run=False,
    )
    t0 = time.time()
    try:
        outcome = await asyncio.wait_for(client.generate(req), timeout=timeout)
        dur = time.time() - t0
        logger.info("图像探针成功! 耗时: {:.2f}s, cost_cny: {}, has_url: {}, has_b64: {}", dur, outcome.cost_cny, bool(outcome.url), bool(outcome.b64))
        return True
    except Exception as exc:
        logger.error("图像探针失败: {}", exc)
        return False


async def verify_video(settings: Settings, timeout: float) -> bool:
    """测试生视频能力 (提交 + 轮询)。"""
    logger.info("=== 测试视频生成 (MiniMax Hailuo / 备用通道) ===")
    if not settings.minimax_api_key:
        logger.warning("MINIMAX_API_KEY 未配置，跳过真实视频供应商探针")
        return True

    client = get_video_client(settings, dry_run=False)
    req = VideoRequest(
        prompt="Slow motion push in, the man turns his head slightly and looks determined.",
        model="hailuo-2.3-fast",
        duration_sec=6.0,
        quality_tier="fast",
        dry_run=False,
    )
    t0 = time.time()
    try:
        provider_task_id = await client.submit(req)
        logger.info("视频任务提交成功! provider_task_id={}", provider_task_id)
        
        # 轮询直至完成或超时
        deadline = time.time() + timeout
        while time.time() < deadline:
            outcome = await client.poll(provider_task_id)
            if outcome.status in {"COMPLETED", "FAILED"}:
                break
            logger.info("视频生成中 (status={})，等待 10s...", outcome.status)
            await asyncio.sleep(10.0)

        dur = time.time() - t0
        logger.info("视频探针完成! 耗时: {:.2f}s, status={}, cost_cny: {}, url: {}", dur, outcome.status, outcome.cost_cny, outcome.url)
        return outcome.status == "COMPLETED"
    except Exception as exc:
        logger.error("视频探针失败: {}", exc)
        return False


async def verify_tts(settings: Settings, timeout: float) -> bool:
    """测试语音合成能力。"""
    logger.info("=== 测试语音合成 (TTS) ===")
    client = get_tts_client(settings, dry_run=False)
    t0 = time.time()
    try:
        audio_bytes = await asyncio.wait_for(client.synthesize("短剧受控生产系统探针测试", voice="default"), timeout=timeout)
        dur = time.time() - t0
        logger.info("TTS 探针完成! 耗时: {:.2f}s, 产生字节数: {}", dur, len(audio_bytes))
        return len(audio_bytes) > 0
    except Exception as exc:
        logger.error("TTS 探针失败: {}", exc)
        return False


async def run_verify(args: argparse.Namespace) -> int:
    """探针执行入口。"""
    settings = get_settings()
    mode = args.mode
    timeout = float(args.timeout)

    success = True
    if mode in ("all", "image"):
        ok = await verify_image(settings, timeout)
        success = success and ok
    if mode in ("all", "video"):
        ok = await verify_video(settings, timeout)
        success = success and ok
    if mode in ("all", "tts"):
        ok = await verify_tts(settings, timeout)
        success = success and ok

    if success:
        logger.info("[ok] 供应商探针全部通过 (或已合法跳过空配置)")
        return 0
    else:
        logger.error("[fail] 存在供应商探针失败项")
        return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(prog="verify_provider", description="真实供应商连通性探针测试")
    parser.add_argument("--mode", choices=["all", "video", "image", "tts"], default="all", help="测试模块")
    parser.add_argument("--timeout", type=float, default=60.0, help="单项测试超时时间 (秒)")
    args = parser.parse_args(argv)
    return asyncio.run(run_verify(args))


if __name__ == "__main__":
    sys.exit(main())
