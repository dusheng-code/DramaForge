"""职责：task / stage_state / resolution 的唯一读写入口（§0.6）：阶段行供给、CAS 迁移、就绪判定、派生任务状态。
输入：TaskSpec、StageRow、CAS 期望值；DSN。
输出：StageRow 列表与布尔 CAS 结果；状态写入只经本模块，禁止任何直接 UPDATE 散落业务代码。

副作用：读写 MySQL（aiomysql）；不写磁盘、不发网络。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..contracts.artifacts import ResolutionRecord, StageRow, TaskRow, TaskSpec
from ..contracts.enums import SLOT_KEYS, STAGES, GLOBAL_SLOT
from .transitions import (
    IllegalTransition,
    assert_stage_transition,
    deps_satisfied,
    gate_slots,
    stage_order,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    """DECIMAL/str/None → float。无副作用。"""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_field(value: Any) -> dict[str, Any]:
    """JSON 列读取：aiomysql 返回 str，兼容已是 dict 的驱动。无副作用。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def stage_row(mapping: Any) -> StageRow:
    """DB 行 → StageRow。无副作用。"""
    m = dict(mapping)
    return StageRow(
        task_id=m["task_id"],
        slot_key=m["slot_key"],
        stage=m["stage"],
        revision=int(m.get("revision") or 0),
        status=m.get("status") or "PENDING",
        fail_reason=m.get("fail_reason"),
        output_path=m.get("output_path"),
    )


def task_row(mapping: Any) -> TaskRow:
    """DB 行 → TaskRow。无副作用。"""
    m = dict(mapping)
    return TaskRow(
        task_id=m["task_id"],
        topic=m.get("topic") or "",
        platform=m.get("platform"),
        episode_count=m.get("episode_count"),
        episode_duration_target=m.get("episode_duration_target"),
        strict_mode=bool(m.get("strict_mode")),
        status=m.get("status") or "PENDING",
        cost_cap_cny=_to_float(m.get("cost_cap_cny"), 0.0) if m.get("cost_cap_cny") is not None else None,
        params=_json_field(m.get("params_json")),
    )


class StateStore:
    """阶段/任务状态仓库。引擎由 connect() 创建，连接池重试由 SQLAlchemy + 调用方共同负责。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    # ── 生命周期 ───────────────────────────────────────────
    @classmethod
    def connect(cls, dsn: str, echo: bool = False) -> "StateStore":
        """建立异步引擎（pool_pre_ping + 回收）。无 IO（惰性连接）。"""
        engine = create_async_engine(
            dsn,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
        )
        return cls(engine)

    async def close(self) -> None:
        """释放连接池。副作用：关闭连接。"""
        await self.engine.dispose()

    async def ping(self) -> bool:
        """探活，失败返回 False（供 /healthz）。副作用：一次查询。"""
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            logger.error("db ping failed: {}", exc)
            return False

    # ── task ───────────────────────────────────────────────
    async def create_task(self, spec: TaskSpec) -> None:
        """写入 task 行并供给 revision 0 的全部阶段行（幂等：重复调用不覆盖已有状态）。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO task (task_id, topic, platform, episode_count, episode_duration_target,
                                      strict_mode, status, cost_cap_cny, params_json)
                    VALUES (:task_id, :topic, :platform, :episode_count, :target, :strict, :status, :cap, :params)
                    ON DUPLICATE KEY UPDATE topic=VALUES(topic)
                    """
                ),
                {
                    "task_id": spec.task_id,
                    "topic": spec.topic[:512],
                    "platform": spec.platform,
                    "episode_count": spec.episode_count,
                    "target": spec.episode_duration_target,
                    "strict": 1 if spec.strict_mode else 0,
                    "status": spec.status,
                    "cap": spec.cost_cap_cny,
                    "params": json.dumps(spec.params, ensure_ascii=False),
                },
            )
        await self.supply_revision(spec.task_id, 0)

    async def get_task(self, task_id: str) -> TaskRow | None:
        """读取任务，不存在返回 None。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(text("SELECT * FROM task WHERE task_id=:t"), {"t": task_id})
            row = res.mappings().first()
        return task_row(row) if row else None

    async def list_tasks(self, limit: int = 50) -> list[TaskRow]:
        """按创建时间倒序列出任务。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text("SELECT * FROM task ORDER BY created_at DESC LIMIT :lim"), {"lim": int(limit)}
            )
            return [task_row(r) for r in res.mappings().all()]

    async def active_task_ids(self) -> list[str]:
        """返回非终态任务 id（恢复用）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text("SELECT task_id FROM task WHERE status NOT IN ('DONE','FAILED') ORDER BY created_at")
            )
            return [r[0] for r in res.all()]

    async def update_task_status(self, task_id: str, status: str) -> None:
        """写 task.status（派生值）。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text("UPDATE task SET status=:s WHERE task_id=:t AND status<>:s"),
                {"s": status, "t": task_id},
            )

    async def update_task_cap(self, task_id: str, cap_cny: float) -> None:
        """人工追加预算：改写 cost_cap_cny。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text("UPDATE task SET cost_cap_cny=:c WHERE task_id=:t"), {"c": cap_cny, "t": task_id}
            )

    async def update_task_params(self, task_id: str, params: dict[str, Any]) -> None:
        """覆盖 task.params_json（如记录 quote 审批额）。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text("UPDATE task SET params_json=:p WHERE task_id=:t"),
                {"p": json.dumps(params, ensure_ascii=False), "t": task_id},
            )

    # ── stage rows ─────────────────────────────────────────
    async def insert_stage_rows(self, rows: Iterable[StageRow]) -> int:
        """批量插入阶段行（INSERT IGNORE，幂等）。副作用：写库。"""
        payload = [
            {
                "task_id": r.task_id,
                "slot_key": r.slot_key,
                "stage": r.stage,
                "revision": r.revision,
                "status": r.status,
                "fail_reason": r.fail_reason,
                "output_path": (r.output_path or "")[:512] or None,
            }
            for r in rows
        ]
        if not payload:
            return 0
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT IGNORE INTO stage_state
                      (task_id, slot_key, stage, revision, status, fail_reason, output_path)
                    VALUES (:task_id, :slot_key, :stage, :revision, :status, :fail_reason, :output_path)
                    """
                ),
                payload,
            )
        return len(payload)

    async def supply_revision(self, task_id: str, revision: int) -> int:
        """为该 revision 补齐全部阶段行：reference/constraint/outline 复用上一 revision 的 PASSED，其余 PENDING。

        副作用：写库。重复调用幂等（INSERT IGNORE）。
        """
        from .transitions import REWORK_PRESERVED

        carried: dict[str, StageRow] = {}
        if revision > 0:
            prev = await self.list_stages(task_id, revision=revision - 1)
            for r in prev:
                if r.stage in REWORK_PRESERVED and r.status == "PASSED":
                    carried[r.stage] = r
        rows: list[StageRow] = []
        for stage in STAGES:
            slots = gate_slots() if stage == "gate" else (GLOBAL_SLOT,)
            for slot in slots:
                src = carried.get(stage) if slot == GLOBAL_SLOT else None
                rows.append(
                    StageRow(
                        task_id=task_id,
                        slot_key=slot,
                        stage=stage,
                        revision=revision,
                        status=src.status if src else "PENDING",
                        output_path=src.output_path if src else None,
                    )
                )
        return await self.insert_stage_rows(rows)

    async def get_stage(
        self, task_id: str, stage: str, slot_key: str = GLOBAL_SLOT, revision: int | None = None
    ) -> StageRow | None:
        """读取单个阶段行；revision 为 None 时取当前最大 revision。副作用：读库。"""
        rev = revision if revision is not None else await self.current_revision(task_id)
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT * FROM stage_state WHERE task_id=:t AND slot_key=:s AND stage=:st AND revision=:r"
                ),
                {"t": task_id, "s": slot_key, "st": stage, "r": rev},
            )
            row = res.mappings().first()
        return stage_row(row) if row else None

    async def list_stages(self, task_id: str, revision: int | None = None) -> list[StageRow]:
        """列出某 revision 的全部阶段行（默认当前 revision），按依赖顺序排序。副作用：读库。"""
        rev = revision if revision is not None else await self.current_revision(task_id)
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text("SELECT * FROM stage_state WHERE task_id=:t AND revision=:r"),
                {"t": task_id, "r": rev},
            )
            rows = [stage_row(r) for r in res.mappings().all()]
        rows.sort(key=lambda r: (stage_order(r.stage), SLOT_KEYS.index(r.slot_key) if r.slot_key in SLOT_KEYS else -1))
        return rows

    async def current_revision(self, task_id: str) -> int:
        """当前最大 revision（无行时为 0）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text("SELECT COALESCE(MAX(revision),0) AS r FROM stage_state WHERE task_id=:t"), {"t": task_id}
            )
            return int(res.scalar() or 0)

    async def stage_status_map(self, task_id: str, revision: int | None = None) -> dict[tuple[str, str], str]:
        """(stage, slot) → status 映射，供依赖判定。副作用：读库。"""
        rows = await self.list_stages(task_id, revision=revision)
        return {(r.stage, r.slot_key): r.status for r in rows}

    async def cas(
        self,
        row: StageRow,
        expect: str,
        new: str,
        fail_reason: str | None = None,
        output_path: str | None = None,
    ) -> bool:
        """比较并交换阶段状态（唯一写状态的方式）。非法迁移抛 IllegalTransition。副作用：写库。"""
        assert_stage_transition(expect, new)
        async with self.engine.begin() as conn:
            res = await conn.execute(
                text(
                    """
                    UPDATE stage_state
                       SET status=:new,
                           fail_reason=:fr,
                           output_path=COALESCE(:op, output_path)
                     WHERE task_id=:t AND slot_key=:s AND stage=:st AND revision=:r AND status=:exp
                    """
                ),
                {
                    "new": new,
                    "fr": (fail_reason or "")[:1024] or None,
                    "op": (output_path or "")[:512] or None,
                    "t": row.task_id,
                    "s": row.slot_key,
                    "st": row.stage,
                    "r": row.revision,
                    "exp": expect,
                },
            )
            ok = res.rowcount == 1
        if ok:
            logger.debug(
                "cas stage {} {} rev={} {}->{}", row.stage, row.slot_key, row.revision, expect, new
            )
        return ok

    async def set_output(self, row: StageRow, output_path: str) -> None:
        """只更新 output_path（不改状态）。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE stage_state SET output_path=:op
                     WHERE task_id=:t AND slot_key=:s AND stage=:st AND revision=:r
                    """
                ),
                {
                    "op": output_path[:512],
                    "t": row.task_id,
                    "s": row.slot_key,
                    "st": row.stage,
                    "r": row.revision,
                },
            )

    async def mark_stale(self, task_id: str, stages: Iterable[str], revision: int) -> int:
        """把指定阶段在该 revision 的行标记 STALE（用于返工）。副作用：写库。"""
        names = list(stages)
        if not names:
            return 0
        async with self.engine.begin() as conn:
            res = await conn.execute(
                text(
                    """
                    UPDATE stage_state SET status='STALE'
                     WHERE task_id=:t AND revision=:r AND status IN ('PENDING','PASSED','FAILED','RUNNING')
                       AND stage IN :stages
                    """
                ).bindparams(bindparam("stages", expanding=True)),
                {"t": task_id, "r": revision, "stages": names},
            )
            return int(res.rowcount or 0)

    async def promote_stale(self, task_id: str) -> int:
        """把依赖已满足的 STALE 行改回 PENDING（STALE→PENDING 只由本处/revision 写）。副作用：写库。

        返回提升行数。
        """
        rev = await self.current_revision(task_id)
        rows = await self.list_stages(task_id, revision=rev)
        status_map = {(r.stage, r.slot_key): r.status for r in rows}
        promoted = 0
        staged = [r for r in rows if r.status == "STALE"]
        for row in staged:
            status_map[(row.stage, row.slot_key)] = "PENDING"
        # 依赖判定需按自上而下顺序推进，故排序后顺序处理
        for row in sorted(staged, key=lambda r: stage_order(r.stage)):
            if deps_satisfied(row.stage, status_map):
                if await self.cas(row, "STALE", "PENDING"):
                    promoted += 1
        return promoted

    async def ready_stages(self, task_id: str) -> list[StageRow]:
        """§6.3 就绪判定：当前 revision 下 PENDING、依赖全 PASSED、且未被同 slot 的 NEEDS_HUMAN 阻塞。副作用：读库。"""
        rev = await self.current_revision(task_id)
        rows = await self.list_stages(task_id, revision=rev)
        status_map = {(r.stage, r.slot_key): r.status for r in rows}
        blocked_slots = {r.slot_key for r in rows if r.status == "NEEDS_HUMAN"}
        ready: list[StageRow] = []
        for row in rows:
            if row.status != "PENDING":
                continue
            if row.slot_key != GLOBAL_SLOT and row.slot_key in blocked_slots:
                continue
            if deps_satisfied(row.stage, status_map):
                ready.append(row)
        return ready

    async def count_by_status(self, task_id: str, revision: int | None = None) -> dict[str, int]:
        """阶段状态直方图（供 /stages 与派生 status）。副作用：读库。"""
        rows = await self.list_stages(task_id, revision=revision)
        out: dict[str, int] = {}
        for r in rows:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    # ── resolution ─────────────────────────────────────────
    async def add_resolution(self, rec: ResolutionRecord) -> None:
        """记录一条人工决议（留痕，不判状态）。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO resolution (task_id, target_type, target_id, action, operator, comment)
                    VALUES (:t, :tt, :ti, :a, :o, :c)
                    """
                ),
                {
                    "t": rec.task_id,
                    "tt": rec.target_type[:16],
                    "ti": rec.target_id[:64],
                    "a": rec.action[:32],
                    "o": rec.operator[:32],
                    "c": (rec.comment or "")[:1024] or None,
                },
            )

    async def list_resolutions(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """倒序列出决议留痕。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT target_type, target_id, action, operator, comment, created_at "
                    "FROM resolution WHERE task_id=:t ORDER BY id DESC LIMIT :lim"
                ),
                {"t": task_id, "lim": int(limit)},
            )
            return [
                {
                    "target_type": r[0],
                    "target_id": r[1],
                    "action": r[2],
                    "operator": r[3],
                    "comment": r[4],
                    "created_at": r[5].isoformat() if isinstance(r[5], (datetime, date)) else str(r[5]),
                }
                for r in res.all()
            ]

    # ── 计数（供 quote / 报告 / 派生 status）────────────────
    async def stage_counts_at_revision(self, task_id: str) -> dict[str, int]:
        """当前 revision 的阶段状态计数（派生 status 用）。副作用：读库。"""
        return await self.count_by_status(task_id)

    def assert_known_slot(self, slot_key: str) -> None:
        """校验 slot_key 合法（'*' 或五个段位之一）。无副作用（只抛异常）。"""
        if slot_key != GLOBAL_SLOT and slot_key not in SLOT_KEYS:
            raise IllegalTransition(f"unknown slot_key {slot_key}")
