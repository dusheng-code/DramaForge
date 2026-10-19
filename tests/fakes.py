"""职责：测试用 Fake 实现的唯一存放处（§0.3：Fake 只允许出现在 tests/ 与 --dry-run 路径，类名带 Fake 前缀）。
输入：fixture 数据、脚本化的成功/失败/超时剧本。
输出：可注入 Scheduler / Pump / agents 的假实现，记录调用次数以支撑 M6/M7 断言。

副作用：只在内存与 pytest tmp 目录内；绝不发真实网络请求、绝不写生产库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from dramaforge.contracts.assets import ImageRequest, ImageResult, VideoOutcome, VideoRequest


class FakeRunner:
    """替代 TaskRunner：只记录 submit_task 调用，不真的起协程。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit_task(self, task_id: str) -> None:
        """记录一次提交。副作用：内存计数。"""
        self.submitted.append(task_id)

    def is_running(self, task_id: str) -> bool:
        """永远返回 False。无副作用。"""
        return False


class FakeLLMClient:
    """按 role + 调用序号回放 fixture JSON 的 LLM 客户端。

    script 形如 {"script.outline": [ {...}, {...} ]}：同名调用按顺序取，用尽后重复最后一个。
    """

    def __init__(self, script: dict[str, list[Any]] | None = None, role: str = "script") -> None:
        self.script = script or {}
        self.role = role
        self.calls: list[tuple[str, str]] = []
        self._cursor: dict[str, int] = {}

    def generate_object(self, system: str, user: str, model_cls, key: str | None = None):
        """同步返回 fixture 对象（已按 model_cls 校验）。无网络副作用。"""
        raise NotImplementedError

    async def agenerate_object(self, system: str, user: str, model_cls, key: str | None = None):
        """异步版本：从 script 取下一个载荷并校验成 model_cls。副作用：内存计数。"""
        name = key or self.role
        self.calls.append((self.role, name))
        payloads = self.script.get(name) or self.script.get(self.role) or []
        idx = self._cursor.get(name, 0)
        if payloads:
            payload = payloads[min(idx, len(payloads) - 1)]
            self._cursor[name] = idx + 1
        else:
            payload = {}
        return _coerce(model_cls, payload)


def _coerce(model_cls, payload: Any):
    """把 fixture 载荷装进 model_cls：list 模型或单对象模型都可。无副作用。"""
    import pydantic

    origin = getattr(model_cls, "__origin__", None)
    if origin in (list, tuple) or str(model_cls).startswith("typing.List"):
        inner = model_cls.__args__[0]
        return [inner.model_validate(item) if isinstance(item, dict) else item for item in payload]
    if isinstance(model_cls, type) and issubclass(model_cls, pydantic.BaseModel):
        return model_cls.model_validate(payload)
    return payload


class FakeVideoClient:
    """脚本化视频客户端：可控制 submit/poll 的成功、失败、超时与计费，并统计调用次数（M7 断言依据）。"""

    name = "fake-video"

    def __init__(
        self,
        *,
        fail_times: int = 0,
        done_after_polls: int = 0,
        cost_cny: float = 0.225,
        timeout_forever: bool = False,
        url: str = "https://fake.local/video.mp4",
    ) -> None:
        self.fail_times = fail_times
        self.done_after_polls = done_after_polls
        self.cost_cny = cost_cny
        self.timeout_forever = timeout_forever
        self.url = url
        self.submit_calls: list[VideoRequest] = []
        self.poll_calls: list[str] = []
        self._polls: dict[str, int] = {}
        self._seq = 0

    async def submit(self, req: VideoRequest) -> str:
        """立即返回句柄；按 fail_times 决定后续失败次数。副作用：内存计数。"""
        self.submit_calls.append(req)
        self._seq += 1
        return f"fake-task-{self._seq}"

    async def poll(self, provider_task_id: str) -> VideoOutcome:
        """按剧本推进状态。副作用：内存计数。"""
        self.poll_calls.append(provider_task_id)
        n = self._polls.get(provider_task_id, 0)
        self._polls[provider_task_id] = n + 1
        if self.timeout_forever:
            return VideoOutcome(done=False, failed=False, status="processing", provider_task_id=provider_task_id)
        if self.fail_times > 0:
            self.fail_times -= 1
            return VideoOutcome(
                done=False,
                failed=True,
                error="provider error (scripted)",
                cost_cny=0.0,
                provider_task_id=provider_task_id,
            )
        if n < self.done_after_polls:
            return VideoOutcome(done=False, failed=False, status="processing", provider_task_id=provider_task_id)
        return VideoOutcome(
            done=True,
            failed=False,
            url=self.url,
            cost_cny=self.cost_cny,
            provider_task_id=provider_task_id,
        )


class FakeImageClient:
    """脚本化图像客户端：把 1x1 PNG 写进 tmp 目录，统计调用次数。"""

    name = "fake-image"
    PNG_1PX = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )

    def __init__(self, cost_cny: float = 0.2, fail_times: int = 0) -> None:
        self.cost_cny = cost_cny
        self.fail_times = fail_times
        self.calls: list[ImageRequest] = []

    async def generate(self, req: ImageRequest) -> ImageResult:
        """返回内联 b64（由 pump 落盘）。副作用：内存计数。"""
        import base64

        self.calls.append(req)
        if self.fail_times > 0:
            self.fail_times -= 1
            return ImageResult(done=False, failed=True, error="image error (scripted)")
        return ImageResult(
            done=True,
            b64=base64.b64encode(self.PNG_1PX).decode("ascii"),
            cost_cny=self.cost_cny,
        )


class FakeDownloader:
    """替代 httpx 下载：把固定字节写到目标路径，统计调用。"""

    def __init__(self, content: bytes = b"fake-binary") -> None:
        self.content = content
        self.calls: list[str] = []

    async def download(self, url: str) -> bytes:
        """返回脚本化字节。无网络副作用。"""
        self.calls.append(url)
        return self.content


def write_json(path: Path, obj: Any) -> Path:
    """测试辅助：直接写 JSON（不经过 io/artifacts，保持测试自洽）。无副作用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def loader(mapping: dict[str, Any]) -> Callable[[str], Any]:
    """测试辅助：按 stage 返回固定 handler 返回值。无副作用。"""

    def _get(stage: str) -> Any:
        return mapping.get(stage)

    return _get
