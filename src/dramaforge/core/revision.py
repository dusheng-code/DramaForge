"""职责：返工闭环（§6.4）：gate 违规 → revision+1、下游 STALE、旧资产 ORPHANED（保留文件与花费）、触顶转人工。
输入：触发派发的阶段行（通常为某个 slot 的 gate）、违规清单。
输出：新的 revision 号或 "NEEDS_HUMAN"；STALE→PENDING 的推进也在此收口。

副作用：写库、写 review/violations.json、发事件、写日志。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from loguru import logger

from ..contracts.artifacts import AssetRow, StageRow, Violation
from ..contracts.enums import AssetKind
from ..io import artifacts as io
from .events import Events
from .state_store import StateStore
from .transitions import REWORK_ENTRY, stages_after

PathFn = Callable[[str], io.TaskPaths]


class RevisionManager:
    """返工与 STALE 推进的唯一实现处。"""

    def __init__(
        self,
        store: StateStore,
        assets,
        settings,
        events: Events,
        path_fn: PathFn = io.paths,
    ) -> None:
        self.store = store
        self.assets = assets
        self.settings = settings
        self.events = events
        self.path_fn = path_fn
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, task_id: str) -> asyncio.Lock:
        """同 task 的派发串行化，避免两个 slot 同时把 revision 推到 +2。无副作用。"""
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    async def dispatch(self, row: StageRow, violations: list[Violation]) -> str:
        """执行一次返工派发。副作用：写库、写 violations.json、发事件。

        返回新 revision（str）或 "NEEDS_HUMAN"（触顶）。
        """
        task_id = row.task_id
        async with self._lock(task_id):
            paths = self.path_fn(task_id).ensure()
            payload = [v.model_dump() for v in violations]
            io.write_json(paths.violations_json, {"task_id": task_id, "revision": row.revision, "violations": payload})
            max_rev = int(self.settings.max_revision_per_slot)
            if row.revision >= max_rev:
                logger.warning(
                    "revision cap reached: task={} rev={} cap={}", task_id, row.revision, max_rev
                )
                await self.store.cas(row, "RUNNING", "NEEDS_HUMAN", fail_reason="revision cap reached")
                self.events.needs_human(task_id, row.stage, "返工次数到顶，需人工决议", row.slot_key)
                return "NEEDS_HUMAN"

            current = await self.store.current_revision(task_id)
            base = max(current, row.revision)
            new_rev = base + 1

            # 1) 新 revision 行：reference/constraint/outline 复用 PASSED，其余 PENDING
            await self.store.supply_revision(task_id, new_rev)
            # 2) 新 revision 中 episodes 之后的下游置 STALE（等依赖满足后由 promote_stale 放行）
            downstream = [s for s in stages_after(outline=False) if s != REWORK_ENTRY]
            staled_new = await self.store.mark_stale(task_id, downstream, new_rev)
            # 3) 旧 revision 的下游行同样标 STALE（历史不可再用）
            staled_old = await self.store.mark_stale(task_id, list(stages_after(outline=False)), row.revision)

            # 4) 受影响 shot / 集
            affected = await self._affected_shots(task_id, violations, row.revision)
            # 5) 旧 revision 资产 → ORPHANED（保留文件与 cost，不参与重算）
            orphaned = await self.assets.orphan(task_id, affected or None, upto_revision=row.revision)
            # 6) 新 revision 资产行以 PENDING 插入（§6.4.2）
            inserted = await self._insert_new_revision_assets(task_id, affected, new_rev, row.revision)

            logger.info(
                "revision dispatched: task={} rev {}->{} violations={} stale(new/old)={}/{} orphaned={} inserted={}",
                task_id,
                row.revision,
                new_rev,
                len(payload),
                staled_new,
                staled_old,
                orphaned,
                inserted,
            )
            self.events.stage_updated(row.model_copy(update={"status": "STALE", "revision": new_rev}))
            return str(new_rev)

    async def promote_stale(self, task_id: str) -> int:
        """把依赖已满足的 STALE 行改回 PENDING（由 Scheduler.loop 每轮调用）。副作用：写库。"""
        promoted = await self.store.promote_stale(task_id)
        if promoted:
            logger.info("promoted {} stale rows: task={}", promoted, task_id)
        return promoted

    # ── 内部 ───────────────────────────────────────────────
    async def _affected_shots(
        self, task_id: str, violations: list[Violation], revision: int
    ) -> list[str]:
        """从违规里收集受影响 shot_id；为空则视为全量镜头受影响。副作用：读磁盘。"""
        shots: list[str] = []
        for v in violations:
            for sid in v.shot_ids:
                if sid not in shots:
                    shots.append(sid)
        if shots:
            return shots
        paths = self.path_fn(task_id)
        raw = io.read_json(paths.shots_json, default={}) or {}
        for item in raw.get("shots", []):
            sid = item.get("shot_id")
            if sid and sid not in shots:
                shots.append(sid)
        return shots

    async def _insert_new_revision_assets(
        self, task_id: str, shot_ids: list[str], new_rev: int, old_rev: int
    ) -> int:
        """为受影响 shot 在新 revision 插入 keyframe/video 的 PENDING 行（幂等）。副作用：写库。"""
        if not shot_ids:
            return 0
        paths = self.path_fn(task_id)
        shots_raw = (io.read_json(paths.shots_json, default={}) or {}).get("shots", [])
        episode_of = {s.get("shot_id"): int(s.get("episode") or 0) for s in shots_raw}
        packs_raw = (io.read_json(paths.packs_json, default={}) or {}).get("packs", [])
        tier_of = {p.get("shot_id"): p.get("tier") for p in packs_raw}
        rows: list[AssetRow] = []
        for sid in shot_ids:
            for kind in ("keyframe", "video"):
                rows.append(
                    AssetRow(
                        task_id=task_id,
                        shot_id=sid,
                        kind=kind,  # type: ignore[arg-type]
                        revision=new_rev,
                        status="PENDING",
                        attempt=0,
                        episode=episode_of.get(sid, 0),
                        tier=tier_of.get(sid),
                    )
                )
        return await self.assets.insert_rows(rows)


def asset_rows_for_quote(
    task_id: str, shots: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], revision: int
) -> list[AssetRow]:
    """按报价单构造 keyframe / video 资产行（供 quote 批准后落库）。无副作用。"""
    rows: list[AssetRow] = []
    for shot in shots:
        sid = str(shot.get("shot_id") or "")
        if not sid:
            continue
        q = quotes.get(sid, {})
        for kind in ("keyframe", "video"):
            rows.append(
                AssetRow(
                    task_id=task_id,
                    shot_id=sid,
                    kind=kind,  # type: ignore[arg-type]
                    revision=revision,
                    status="PENDING",
                    attempt=0,
                    episode=int(shot.get("episode") or 0),
                    tier=shot.get("quality_tier"),
                    est_cost_cny=float(q.get(kind, 0.0) or 0.0),
                )
            )
    return rows


def kind_of(name: str) -> AssetKind:
    """字符串转 AssetKind（类型辅助）。无副作用。"""
    return name  # type: ignore[return-value]
