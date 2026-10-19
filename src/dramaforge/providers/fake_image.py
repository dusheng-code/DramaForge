"""职责：`--dry-run` 与 M8 之前的占位图像客户端（§0.3/§10.4）：返回内联 1×1 PNG 的 base64，并记录调用次数与脚本化失败。
输入：ImageRequest、构造参数（fail_times / cost_cny）。
输出：ImageResult（done=True 时 b64 为真实可解码 PNG；failed=True 时带 error）；calls 供测试断言。

副作用：仅内存计数，不发网络请求、不写磁盘；不联网、不落盘（落盘由 assets/pump 经 io/artifacts 完成）。
"""

from __future__ import annotations

import base64

from loguru import logger

from ..contracts.assets import ImageRequest, ImageResult

# 真实可解码的 1×1 PNG（69 字节，8bit RGB，白色）：PNG 签名 + IHDR + IDAT + IEND，CRC 均已校验。
FAKE_PNG_B64: str = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mP4//8/AAX+Av4zEpUUAAAAAElFTkSuQmCC"
)


def fake_png_bytes() -> bytes:
    """返回 1×1 PNG 的原始字节（供测试确认 base64 可解码）。无副作用。"""
    return base64.b64decode(FAKE_PNG_B64)


class FakeImageClient:
    """占位图像客户端：不花钱、不联网，只为让 --dry-run 与 M8 之前的链路跑通。"""

    name: str = "fake-image"

    def __init__(self, fail_times: int = 0, cost_cny: float = 0.0) -> None:
        """按脚本参数构造；默认 cost_cny=0 对齐 --dry-run 零花费。无副作用。"""
        self.fail_times = max(0, int(fail_times))
        self.cost_cny = round(float(cost_cny), 3)
        self.calls: list[ImageRequest] = []

    async def generate(self, req: ImageRequest) -> ImageResult:
        """返回内联 1×1 PNG base64；前 fail_times 次调用按脚本返回 failed。副作用：内存计数。"""
        self.calls.append(req)
        attempt = len(self.calls)
        if attempt <= self.fail_times:
            logger.debug("FakeImageClient 脚本化失败 kind={} shot={}", req.kind, req.shot_id or "-")
            return ImageResult(
                done=False,
                failed=True,
                error=f"脚本化失败（fail_times={self.fail_times}）",
                cost_cny=0.0,
                raw={"fake": True, "attempt": attempt},
            )
        logger.debug(
            "FakeImageClient 产出占位图 kind={} shot={} attempt={}",
            req.kind,
            req.shot_id or "-",
            attempt,
        )
        return ImageResult(
            done=True,
            failed=False,
            b64=FAKE_PNG_B64,
            cost_cny=self.cost_cny,
            raw={"fake": True, "attempt": attempt, "note": "1x1 PNG 占位，M8 换真实图像"},
        )
