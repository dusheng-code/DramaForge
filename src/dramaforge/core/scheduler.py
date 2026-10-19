"""职责：阶段调度器（§6.3）：就绪扫描、CAS 抢占、按 stage 分发到 handler、异常归口，以及 task 级提交与收尾。
输入：StateStore / Events / RevisionManager / handlers 字典（stage → async 可调用）。
输出：被推进一步的阶段行；任务终态（DONE / FAILED / 等待人工）。

副作用：写库、发事件、写日志。异常禁止向上抛（§6.3 明确 logger.exception 后置 FAILED）。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from ..contracts.artifacts import StageRow
from ..contracts.enums import GLOBAL_SLOT, STAGES
from .events import Events
from .state_store import StateStore
from .transitions import GateViolation, NeedsHuman, derive_task_status

Handler = Callable[[StageRow], Awaitable[str | None]]


class Scheduler:
    """单实例协程调度器；同一 task 只应有一个 loop 在跑（submit_task 负责去重）。"""

    def __init__(
        self,
        store: StateStore,
        events: Events,
        handlers: dict[str, Handler],
        revision_manager,
        *,
        asset_counts: Callable[[str], Awaitable[dict[str, int]]] | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        self.store = store
        self.events = events
        self.handlers = handlers
        self.revision = revision_manager
        self._asset_counts = asset_counts
        self.poll_interval = poll_interval
        self._loops: dict[str, asyncio.Task] = {}

    # ── 主循环 ─────────────────────────────────────────────
    async def loop(self, task_id: str) -> None:
        """反复推进就绪阶段直到没有可推进项；不抛出异常。副作用：写库/发事件。"""
        while True:
            try:
                await self.store.promote_stale(task_id)
                rows = await self.store.ready_stages(task_id)
            except SQLAlchemyError as exc:
                logger.error("ready_stages failed after retries: {}", exc)
                await self._fail_all_running(task_id, f"db error: {exc}")
                return
            if not rows:
                await self._settle(task_id)
                return
            await asyncio.gather(*(self.run_one(r) for r in rows), return_exceptions=True)
            await asyncio.sleep(self.poll_interval)

    async def run_one(self, row: StageRow) -> None:
        """执行单个阶段：CAS 抢占 → handler → PASSED；三类受控异常分流。副作用：写库/发事件。"""
        if not await self.store.cas(row, "PENDING", "RUNNING"):
            return
        running = row.model_copy(update={"status": "RUNNING"})
        self.events.stage_updated(running)
        handler = self.handlers.get(row.stage)
        if handler is None:
            logger.error("no handler for stage {}", row.stage)
            await self.store.cas(row, "RUNNING", "FAILED", fail_reason=f"no handler for {row.stage}")
            return
        try:
            output = await handler(row)
            if output:
                await self.store.set_output(row, output)
            await self.store.cas(row, "RUNNING", "PASSED", output_path=output)
            self.events.stage_updated(
                row.model_copy(update={"status": "PASSED", "output_path": output})
            )
        except GateViolation as exc:
            logger.warning("gate violation on {}: {}", row.stage, len(exc.violations))
            await self.revision.dispatch(row, exc.violations)
        except NeedsHuman as exc:
            await self.store.cas(row, "RUNNING", "NEEDS_HUMAN", fail_reason=str(exc))
            self.events.needs_human(row.task_id, row.stage, str(exc), row.slot_key)
        except asyncio.CancelledError:
            await self.store.cas(row, "RUNNING", "PENDING", fail_reason="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 —— §6.3：禁止向上 raise
            logger.exception("stage failed: {} {}", row.stage, row.slot_key)
            await self.store.cas(row, "RUNNING", "FAILED", fail_reason=str(exc)[:1024])

    # ── 收尾 ───────────────────────────────────────────────
    async def _settle(self, task_id: str) -> None:
        """没有就绪阶段时重算派生 status；全部 PASSED 则置 DONE 并收尾。副作用：写库/发事件。"""
        stages = await self.store.list_stages(task_id)
        asset_statuses = await self._asset_status_list(task_id)
        status = derive_task_status([s.status for s in stages], asset_statuses)
        await self.store.update_task_status(task_id, status)
        if status == "DONE":
            self.events.task_finished(task_id, "DONE")

    async def _asset_status_list(self, task_id: str) -> list[str]:
        """资产状态列表（用于派生 status）。副作用：读库。"""
        if self._asset_counts is None:
            return []
        counts = await self._asset_counts(task_id)
        out: list[str] = []
        for status, n in counts.items():
            out.extend([status] * n)
        return out

    async def _fail_all_running(self, task_id: str, reason: str) -> None:
        """DB 不可用时把仍在 RUNNING 的行置 FAILED（不跳过状态写入，§3.1）。副作用：写库。"""
        for row in await self.store.list_stages(task_id):
            if row.status == "RUNNING":
                await self.store.cas(row, "RUNNING", "FAILED", fail_reason=reason[:1024])
        await self.store.update_task_status(task_id, "FAILED")


class TaskRunner:
    """task 级 run 管理：保证每个 task 只有一个 scheduler/pump 组合在跑（重启幂等）。"""

    def __init__(self, scheduler: Scheduler, pump, store: StateStore, events: Events) -> None:
        self.scheduler = scheduler
        self.pump = pump
        self.store = store
        self.events = events
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit_task(self, task_id: str) -> None:
        """启动（或复用）该 task 的调度 + 资产泵协程。副作用：创建 asyncio 任务。"""
        existing = self._tasks.get(task_id)
        if existing and not existing.done():
            return
        self._tasks[task_id] = asyncio.create_task(self._run(task_id), name=f"dramaforge:{task_id}")

    async def _run(self, task_id: str) -> None:
        """并行跑调度与资产泵；任一先结束时取消另一个并做最终派生。副作用：写库。"""
        sched = asyncio.create_task(self.scheduler.loop(task_id), name=f"sched:{task_id}")
        pump = asyncio.create_task(self.pump.pump(task_id), name=f"pump:{task_id}")
        try:
            while True:
                done, _ = await asyncio.wait({sched, pump}, return_when=asyncio.FIRST_COMPLETED)
                if sched in done and pump in done:
                    break
                if sched in done:
                    if sched.exception():
                        logger.error("scheduler crashed: {}", sched.exception())
                    # 调度器退出后泵继续跑（等待人工/在途资产），避免丢句柄
                    await pump
                    break
                if pump in done:
                    if pump.exception():
                        logger.error("pump crashed: {}", pump.exception())
                    await sched
                    break
        except asyncio.CancelledError:
            sched.cancel()
            pump.cancel()
            raise
        finally:
            for task in (sched, pump):
                if not task.done():
                    task.cancel()
        self._tasks.pop(task_id, None)

    def is_running(self, task_id: str) -> bool:
        """该 task 是否已有协程组在跑。无副作用。"""
        task = self._tasks.get(task_id)
        return bool(task and not task.done())

    async def shutdown(self) -> None:
        """进程退出时取消全部 task。副作用：取消 asyncio 任务。"""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def stage_list() -> tuple[str, ...]:
    """全部阶段名（供 API 展示顺序）。无副作用。"""
    return STAGES


def is_global_slot(slot_key: str) -> bool:
    """是否全局阶段行。无副作用。"""
    return slot_key == GLOBAL_SLOT
