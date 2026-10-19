"""职责：`--dry-run` 与 M7 集成测试用的脚本化视频客户端（§10.4/§17.2）：成功、失败、超时、计费全部按构造参数回放，并记录调用次数供断言。
输入：构造参数（fail_times / done_after_polls / cost_cny / timeout_forever / url）与 VideoRequest。
输出：submit 返回可预测的假句柄；poll 返回三态 VideoOutcome；submit_calls / poll_calls / poll_costs 供 M7 断言。

副作用：仅内存计数，不发网络请求、不写磁盘（§0.3 Fake 只允许出现在 tests/ 与 --dry-run 路径，类名带 Fake）。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from ..contracts.assets import VideoOutcome, VideoRequest

# 假句柄前缀（便于日志一眼看出不是真实 provider）
FAKE_HANDLE_PREFIX = "fake-video-task"


@dataclass
class _FakeHandle:
    """单个假任务的脚本状态：poll 次数、是否已失败、是否已 done、是否已计费。"""

    task_id: str
    request: VideoRequest
    polls: int = 0
    should_fail: bool = False
    failed: bool = False
    done: bool = False
    billed: bool = False
    error: str = ""


class FakeVideoClient:
    """脚本化视频 provider：按构造参数模拟成功/失败/超时与计费，全部在内存内完成。

    脚本语义：
    - `fail_times`：前 N 次 submit 产生的任务在**首次 poll** 即返回 failed=True（用来演练 M7 的重抽与放弃镜头）。
    - `done_after_polls`：同一句柄第 N 次 poll 起返回 done=True，之后保持 done（幂等，不重复计费）。
    - `timeout_forever=True`：poll 永远返回 done=False / failed=False（模拟超时，用于 POLL_TIMEOUT_SEC 分支）。
    - `cost_cny`：只在首次 done 时返回并记账；pending 与 failed 都返回 0.0（§14.4 失败是否计费未核实，不臆造费用）。
    """

    name: str = "fake-video"

    def __init__(
        self,
        fail_times: int = 0,
        done_after_polls: int = 2,
        cost_cny: float = 0.0,
        timeout_forever: bool = False,
        url: str = "fake://video/out.mp4",
    ) -> None:
        """按脚本参数构造；默认 cost_cny=0 对齐 --dry-run 零花费，演练计费时显式传入。无副作用。"""
        self.fail_times = max(0, int(fail_times))
        self.done_after_polls = max(1, int(done_after_polls))
        self.cost_cny = round(float(cost_cny), 3)
        self.timeout_forever = bool(timeout_forever)
        self.url = url
        # M7 断言用：超预算后 submit 调用次数必须为 0；重启后 poll 被调用而 submit 未被调用。
        self.submit_calls: list[VideoRequest] = []
        self.poll_calls: list[str] = []
        # 每次 poll 记一笔花费（pending 记 0），供成本累计断言。
        self.poll_costs: list[float] = []
        self._handles: dict[str, _FakeHandle] = {}
        self._seq = 0

    async def submit(self, req: VideoRequest) -> str:
        """登记一次提交并返回假句柄；前 fail_times 个句柄会在首次 poll 失败。副作用：内存计数。"""
        self._seq += 1
        task_id = f"{FAKE_HANDLE_PREFIX}-{self._seq}"
        self.submit_calls.append(req)
        self._handles[task_id] = _FakeHandle(
            task_id=task_id,
            request=req,
            should_fail=self._seq <= self.fail_times,
        )
        logger.debug(
            "FakeVideoClient.submit shot={} handle={} seq={}",
            req.shot_id or "-",
            task_id,
            self._seq,
        )
        return task_id

    async def poll(self, provider_task_id: str) -> VideoOutcome:
        """按脚本推进该句柄：pending / done / failed 三态之一。

        `.done=False, .failed=False` 表示"仍在生成"（§10.4）。副作用：内存计数与 poll_costs 记账。
        """
        self.poll_calls.append(provider_task_id)
        handle = self._handles.get(provider_task_id)
        if handle is None:
            # 未知句柄（例如指向别的进程/测试的假句柄）：按失败回执处理，不抛栈。
            self.poll_costs.append(0.0)
            logger.warning("FakeVideoClient.poll 收到未知句柄 {}", provider_task_id)
            return VideoOutcome(
                failed=True,
                error=f"未知的假任务句柄：{provider_task_id}",
                provider_task_id=provider_task_id,
                status="fake-unknown",
            )

        if handle.done:
            # 幂等：已 done 的句柄重复 poll 仍返回 done，但不重复计费。
            self.poll_costs.append(0.0)
            return self._outcome(handle, done=True, cost=0.0, status="fake-done")
        if handle.failed:
            self.poll_costs.append(0.0)
            return self._outcome(handle, failed=True, cost=0.0, status="fake-failed")

        handle.polls += 1

        if handle.should_fail:
            handle.failed = True
            handle.error = f"脚本化失败（fail_times={self.fail_times}）"
            return self._settle(handle, failed=True, error=handle.error, status="fake-failed")

        if self.timeout_forever:
            self.poll_costs.append(0.0)
            return self._outcome(handle, status="fake-pending")

        if handle.polls >= self.done_after_polls:
            handle.done = True
            return self._settle(handle, done=True, cost=self.cost_cny, status="fake-done")

        self.poll_costs.append(0.0)
        return self._outcome(handle, status="fake-pending")

    @property
    def total_cost_cny(self) -> float:
        """假客户端累计计费（= poll_costs 之和），供测试对账。无副作用。"""
        return round(sum(self.poll_costs), 3)

    def _settle(
        self,
        handle: _FakeHandle,
        *,
        done: bool = False,
        failed: bool = False,
        cost: float = 0.0,
        error: str = "",
        status: str = "",
    ) -> VideoOutcome:
        """一次结算：记账（同一句柄只结一次）并产出终态回执。副作用：写 poll_costs / billed。"""
        if handle.billed:
            cost = 0.0
        else:
            handle.billed = True
        self.poll_costs.append(round(cost, 3))
        outcome = self._outcome(
            handle, done=done, failed=failed, cost=cost, error=error, status=status
        )
        if cost:
            logger.info(
                "FakeVideoClient 结算 shot={} handle={} cost={} done={} failed={}",
                handle.request.shot_id or "-",
                handle.task_id,
                round(cost, 3),
                done,
                failed,
            )
        return outcome

    def _outcome(
        self,
        handle: _FakeHandle,
        *,
        done: bool = False,
        failed: bool = False,
        cost: float = 0.0,
        error: str = "",
        status: str = "",
    ) -> VideoOutcome:
        """组装 VideoOutcome（url 只在 done 时给）。无副作用。"""
        return VideoOutcome(
            done=done,
            failed=failed,
            cost_cny=round(float(cost), 3),
            url=self.url if done else None,
            error=error or None,
            provider_task_id=handle.task_id,
            status=status,
        )
