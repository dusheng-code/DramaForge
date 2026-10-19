"""职责：进程启动自愈（§6.5）：RUNNING→PENDING、产物校验（缺失标 STALE）、资产续轮询、任务派生状态重算与续跑。
输入：StateStore / AssetStore / Settings / Events / TaskRunner。
输出：每个任务的修复动作统计；非终态任务重新交给 submit_task。

副作用：写库、写 state/*.json 快照、写日志。不重新提交任何已拿到 provider_task_id 的资产。
"""

from __future__ import annotations

from typing import Any, Callable

from loguru import logger

from ..io import artifacts as io
from .events import Events
from .state_store import StateStore
from .transitions import derive_task_status

PathFn = Callable[[str], io.TaskPaths]


class Recovery:
    """启动自愈器；`run()` 由 lifespan 的 on_startup 调用。"""

    def __init__(
        self,
        store: StateStore,
        assets,
        settings,
        events: Events,
        runner,
        path_fn: PathFn = io.paths,
    ) -> None:
        self.store = store
        self.assets = assets
        self.settings = settings
        self.events = events
        self.runner = runner
        self.path_fn = path_fn

    async def run(self) -> dict[str, Any]:
        """扫描全部非终态任务并自愈，返回统计。副作用：写库/写快照/写日志。"""
        stats: dict[str, Any] = {"tasks": 0, "reset_stage": 0, "stale_stage": 0, "resubmitted": 0}
        for task_id in await self.store.active_task_ids():
            stats["tasks"] += 1
            try:
                one = await self.recover_task(task_id)
            except Exception as exc:  # noqa: BLE001 —— 启动自愈不能因单任务失败而中断
                logger.exception("recovery failed for task {}: {}", task_id, exc)
                continue
            for key in ("reset_stage", "stale_stage"):
                stats[key] += one.get(key, 0)
            if one.get("resubmitted"):
                stats["resubmitted"] += 1
        logger.info("recovery done: {}", stats)
        return stats

    async def recover_task(self, task_id: str, resubmit: bool = True) -> dict[str, Any]:
        """单任务自愈。副作用：写库/写快照。

        - stage RUNNING → PENDING；PASSED 但产物缺失 → STALE（§3.1 产物丢失重跑）
        - asset SUBMITTED/GENERATING 保留句柄续轮询；REVIEW 不动；PENDING 不动
        - task 重算派生 status；非终态重新交给 submit_task
        """
        out: dict[str, Any] = {"task_id": task_id, "reset_stage": 0, "stale_stage": 0, "resubmitted": False}
        stages = await self.store.list_stages(task_id)
        for row in stages:
            if row.status == "RUNNING":
                ok = await self.store.cas(row, "RUNNING", "PENDING", fail_reason="recovered after restart")
                if ok:
                    out["reset_stage"] += 1
                    logger.warning("stage reset RUNNING→PENDING: {} {} rev={}", row.stage, row.slot_key, row.revision)
                if row.output_path and not io.output_ok(row.output_path):
                    logger.warning("output missing for {} {}: {}", row.stage, row.slot_key, row.output_path)
            elif row.status == "PASSED" and row.output_path and not io.output_ok(row.output_path):
                ok = await self.store.cas(row, "PASSED", "STALE", fail_reason="output missing")
                if ok:
                    out["stale_stage"] += 1
                    logger.warning("stage STALE due to missing output: {} {}", row.stage, row.slot_key)
        await self._snapshot(task_id)
        stages = await self.store.list_stages(task_id)
        asset_counts = await self.assets.counts_by_status(task_id)
        status = derive_task_status([s.status for s in stages], self._expand(asset_counts))
        await self.store.update_task_status(task_id, status)
        if status not in {"DONE", "FAILED"} and resubmit:
            await self.runner.submit_task(task_id)
            out["resubmitted"] = True
            logger.info("task resubmitted after recovery: {} status={}", task_id, status)
        return out

    async def _snapshot(self, task_id: str) -> None:
        """写 state/stage_snapshot.json 与 asset_snapshot.json（供 /stages 快速视图与复盘）。副作用：写磁盘。"""
        paths = self.path_fn(task_id).ensure()
        stages = await self.store.list_stages(task_id)
        assets = await self.assets.list_assets(task_id)
        io.write_json(
            paths.stage_snapshot_json,
            {
                "task_id": task_id,
                "revision": await self.store.current_revision(task_id),
                "stages": [s.model_dump() for s in stages],
            },
        )
        io.write_json(
            paths.asset_snapshot_json,
            {"task_id": task_id, "assets": [a.model_dump() for a in assets]},
        )

    @staticmethod
    def _expand(counts: dict[str, int]) -> list[str]:
        """把状态直方图展开成列表（派生 status 的输入形状）。无副作用。"""
        out: list[str] = []
        for status, n in counts.items():
            out.extend([status] * int(n))
        return out
