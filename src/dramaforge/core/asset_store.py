"""职责：asset_state 的唯一读写入口（§0.6 / §10）：原子 claim（FOR UPDATE SKIP LOCKED）、迁移、预算账本、放弃清单。
输入：AssetRow、目标状态、金额、shot 列表。
输出：被 claim 到的资产行、预算快照、资产清单与直方图。

副作用：读写 MySQL；不写磁盘、不发网络。资产文件落盘由 pump 经 io/artifacts 完成。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..contracts.artifacts import AssetRow
from ..contracts.assets import BudgetSnapshot
from ..contracts.enums import ASSET_KINDS, ASSET_RESERVED_STATUSES
from .transitions import IllegalTransition, assert_asset_transition

_KIND_ORDER_SQL = "FIELD(kind, 'anchor_image','keyframe','video')"


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


def asset_row(mapping: Any) -> AssetRow:
    """DB 行 → AssetRow。无副作用。"""
    m = dict(mapping)
    return AssetRow(
        task_id=m["task_id"],
        shot_id=m["shot_id"],
        kind=m["kind"],
        revision=int(m.get("revision") or 0),
        status=m.get("status") or "PENDING",
        attempt=int(m.get("attempt") or 0),
        episode=int(m.get("episode") or 0),
        tier=m.get("tier"),
        model=m.get("model"),
        resolution=m.get("resolution"),
        provider=m.get("provider"),
        provider_task_id=m.get("provider_task_id"),
        cost_cny=_to_float(m.get("cost_cny")),
        file_path=m.get("file_path"),
        est_cost_cny=_to_float(m.get("est_cost_cny")) if m.get("est_cost_cny") is not None else None,
        reject_reason=m.get("reject_reason"),
    )


class AssetStore:
    """资产行仓库。所有状态写入都经 mark/cas 系列方法，禁止外部直接 UPDATE。"""

    def __init__(self, engine: AsyncEngine, max_attempt: int = 3) -> None:
        self.engine = engine
        self.max_attempt = max_attempt

    @classmethod
    def connect(cls, dsn: str, max_attempt: int = 3) -> "AssetStore":
        """建立独立异步引擎（泵与调度器共用同一 DSN）。无 IO（惰性连接）。"""
        engine = create_async_engine(
            dsn,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
        )
        return cls(engine, max_attempt=max_attempt)

    async def close(self) -> None:
        """释放连接池。副作用：关闭连接。"""
        await self.engine.dispose()

    # ── 写入 ───────────────────────────────────────────────
    async def insert_rows(self, rows: Iterable[AssetRow]) -> int:
        """批量插入资产行（INSERT IGNORE，幂等）。副作用：写库。"""
        payload = [
            {
                "task_id": r.task_id,
                "shot_id": r.shot_id[:64],
                "kind": r.kind,
                "revision": int(r.revision),
                "status": r.status,
                "attempt": int(r.attempt),
                "episode": int(r.episode),
                "tier": r.tier,
                "model": r.model,
                "resolution": r.resolution,
                "provider": r.provider,
                "provider_task_id": r.provider_task_id,
                "cost_cny": r.cost_cny,
                "file_path": r.file_path,
                "est_cost_cny": r.est_cost_cny,
                "reject_reason": r.reject_reason,
            }
            for r in rows
        ]
        if not payload:
            return 0
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT IGNORE INTO asset_state
                      (task_id, shot_id, kind, revision, status, attempt, episode, tier, model,
                       resolution, provider, provider_task_id, cost_cny, file_path, est_cost_cny, reject_reason)
                    VALUES (:task_id, :shot_id, :kind, :revision, :status, :attempt, :episode, :tier, :model,
                            :resolution, :provider, :provider_task_id, :cost_cny, :file_path, :est_cost_cny,
                            :reject_reason)
                    """
                ),
                payload,
            )
        return len(payload)

    async def claim(self, task_id: str, limit: int = 3, kind: str | None = None) -> list[AssetRow]:
        """原子占位：PENDING → SUBMITTED（FOR UPDATE SKIP LOCKED，§10.3）。副作用：写库。

        排序顺序 = 定妆图 → 首帧 → 视频，再按 episode、shot_id。
        """
        where = "task_id=:t AND status='PENDING'"
        params: dict[str, Any] = {"t": task_id, "n": int(limit)}
        if kind:
            where += " AND kind=:k"
            params["k"] = kind
        async with self.engine.begin() as conn:
            res = await conn.execute(
                text(
                    f"""
                    SELECT * FROM asset_state
                     WHERE {where}
                     ORDER BY {_KIND_ORDER_SQL}, episode, shot_id
                     LIMIT :n
                     FOR UPDATE SKIP LOCKED
                    """
                ),
                params,
            )
            rows = [asset_row(r) for r in res.mappings().all()]
            if not rows:
                return []
            await conn.execute(
                text(
                    """
                    UPDATE asset_state SET status='SUBMITTED'
                     WHERE task_id=:t AND shot_id=:s AND kind=:k AND revision=:r AND status='PENDING'
                    """
                ),
                [
                    {"t": r.task_id, "s": r.shot_id, "k": r.kind, "r": r.revision}
                    for r in rows
                ],
            )
        for r in rows:
            r.status = "SUBMITTED"
        return rows

    async def mark(self, row: AssetRow, status: str, **fields: Any) -> bool:
        """迁移资产状态（校验迁移合法性），可附带列更新。副作用：写库。"""
        assert_asset_transition(row.status, status)
        sets = ["status=:status"]
        params: dict[str, Any] = {
            "t": row.task_id,
            "s": row.shot_id,
            "k": row.kind,
            "r": row.revision,
            "exp": row.status,
            "status": status,
        }
        for key, value in fields.items():
            if key in {"attempt", "provider_task_id", "cost_cny", "file_path", "est_cost_cny", "reject_reason", "model", "tier", "resolution", "provider", "episode"}:
                sets.append(f"{key}=:{key}")
                params[key] = value
        async with self.engine.begin() as conn:
            res = await conn.execute(
                text(
                    f"""
                    UPDATE asset_state SET {', '.join(sets)}
                     WHERE task_id=:t AND shot_id=:s AND kind=:k AND revision=:r AND status=:exp
                    """
                ),
                params,
            )
            ok = res.rowcount == 1
        if ok:
            row.status = status
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            logger.debug("asset {} {} {} -> {}", row.shot_id, row.kind, params["exp"], status)
        return ok

    async def mark_submitted(self, row: AssetRow, provider_task_id: str, model: str | None = None) -> bool:
        """落 provider_task_id 并把 attempt 加一（同事务语义）。副作用：写库。"""
        return await self.mark(
            row,
            "SUBMITTED",
            provider_task_id=provider_task_id,
            attempt=row.attempt + 1,
            model=model or row.model,
        )

    async def save_file(self, row: AssetRow, file_path: str, cost_cny: float) -> bool:
        """下载落盘后置 REVIEW（机器不判可用性）。副作用：写库（文件由调用方写入）。"""
        return await self.mark(
            row, "REVIEW", file_path=file_path[:512], cost_cny=round(float(cost_cny), 3)
        )

    async def retry_or_reject(self, row: AssetRow, cost_cny: float = 0.0, reason: str | None = None) -> str:
        """失败处理：先落 REJECTED，未达 MAX_ATTEMPT 再按 §6.2 走 REJECTED→PENDING(attempt+1)。

        副作用：写库。返回最终状态字符串（PENDING / REJECTED）。
        """
        attempt = row.attempt if row.provider_task_id else row.attempt + 1
        total_cost = round(float(row.cost_cny or 0) + float(cost_cny or 0), 3)
        if row.status != "REJECTED":
            if row.status == "ORPHANED":
                return "ORPHANED"
            await self.mark(
                row,
                "REJECTED",
                attempt=attempt,
                cost_cny=total_cost,
                reject_reason=(reason or "failed")[:512],
            )
        if attempt >= self.max_attempt:
            logger.warning("asset rejected after {} attempts: {} {}", attempt, row.shot_id, row.kind)
            return "REJECTED"
        await self.mark(
            row,
            "PENDING",
            attempt=attempt,
            cost_cny=total_cost,
            reject_reason=(reason or "failed")[:512],
            provider_task_id=None,
        )
        return "PENDING"

    async def hold(self, row: AssetRow) -> bool:
        """预算触顶：置 BUDGET_HOLD。副作用：写库。"""
        return await self.mark(row, "BUDGET_HOLD")

    async def release(self, row: AssetRow) -> bool:
        """占位回退（如视频上游首帧未通过）：SUBMITTED → PENDING，不消耗 attempt。副作用：写库。"""
        return await self.mark(row, "PENDING")

    async def pass_row(self, row: AssetRow) -> bool:
        """人工通过。副作用：写库。"""
        return await self.mark(row, "PASSED")

    async def reject_row(self, row: AssetRow, reason: str = "manual reject") -> str:
        """人工驳回：未达上限回 PENDING（重抽），达上限置 REJECTED。副作用：写库。"""
        return await self.retry_or_reject(row, 0.0, reason)

    async def release_holds(self, task_id: str) -> int:
        """追加预算后释放全部 BUDGET_HOLD 行（回 PENDING 重新排队）。副作用：写库。"""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                text("UPDATE asset_state SET status='PENDING' WHERE task_id=:t AND status='BUDGET_HOLD'"),
                {"t": task_id},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def orphan(self, task_id: str, shot_ids: list[str] | None = None, upto_revision: int | None = None) -> int:
        """上游 revision 变更：把非 PASSED 与 PASSED 的旧行统一置 ORPHANED，保留文件与 cost。

        副作用：写库。返回受影响行数。
        """
        where = "task_id=:t AND status<>'ORPHANED'"
        params: dict[str, Any] = {"t": task_id}
        if shot_ids:
            where += " AND shot_id IN :shots"
        if upto_revision is not None:
            where += " AND revision<=:rev"
            params["rev"] = int(upto_revision)
        stmt = text(f"UPDATE asset_state SET status='ORPHANED' WHERE {where}")
        if shot_ids:
            stmt = stmt.bindparams(bindparam("shots", expanding=True))
            params["shots"] = shot_ids
        async with self.engine.begin() as conn:
            res = await conn.execute(stmt, params)
            return int(res.rowcount or 0)

    async def set_est_cost(self, task_id: str, shot_id: str, kind: str, revision: int, est_cost: float) -> None:
        """写入预估花费（报价单落库），预算 reserved 依赖它。副作用：写库。"""
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE asset_state SET est_cost_cny=:e
                     WHERE task_id=:t AND shot_id=:s AND kind=:k AND revision=:r
                    """
                ),
                {"e": round(float(est_cost), 3), "t": task_id, "s": shot_id, "k": kind, "r": revision},
            )

    # ── 读取 ───────────────────────────────────────────────
    async def list_assets(
        self, task_id: str, revision: int | None = None, kind: str | None = None
    ) -> list[AssetRow]:
        """列出资产行（默认全部 revision）。副作用：读库。"""
        where = "task_id=:t"
        params: dict[str, Any] = {"t": task_id}
        if revision is not None:
            where += " AND revision=:r"
            params["r"] = int(revision)
        if kind is not None:
            where += " AND kind=:k"
            params["k"] = kind
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(f"SELECT * FROM asset_state WHERE {where} ORDER BY {_KIND_ORDER_SQL}, episode, shot_id"),
                params,
            )
            return [asset_row(r) for r in res.mappings().all()]

    async def get_asset(self, task_id: str, shot_id: str, kind: str) -> AssetRow | None:
        """取某 shot 的某类资产（最大 revision）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT * FROM asset_state WHERE task_id=:t AND shot_id=:s AND kind=:k "
                    "ORDER BY revision DESC LIMIT 1"
                ),
                {"t": task_id, "s": shot_id, "k": kind},
            )
            row = res.mappings().first()
        return asset_row(row) if row else None

    async def keyframe_passed(self, task_id: str, shot_id: str) -> bool:
        """图筛闸：该镜是否存在 PASSED 的首帧（§10.3）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT 1 FROM asset_state WHERE task_id=:t AND shot_id=:s AND kind='keyframe' "
                    "AND status='PASSED' LIMIT 1"
                ),
                {"t": task_id, "s": shot_id},
            )
            return res.first() is not None

    async def max_passed_revision(self, task_id: str, kind: str | None = None) -> int:
        """当前最大 PASSED revision（预算与 keyframe 统计口径）。副作用：读库。"""
        where = "task_id=:t AND status='PASSED'"
        params: dict[str, Any] = {"t": task_id}
        if kind:
            where += " AND kind=:k"
            params["k"] = kind
        async with self.engine.connect() as conn:
            res = await conn.execute(text(f"SELECT COALESCE(MAX(revision),0) FROM asset_state WHERE {where}"), params)
            return int(res.scalar() or 0)

    async def list_by_status(self, task_id: str, statuses: Iterable[str]) -> list[AssetRow]:
        """按状态集合列出资产行。副作用：读库。"""
        names = list(statuses)
        if not names:
            return []
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT * FROM asset_state WHERE task_id=:t AND status IN :sts "
                    f"ORDER BY {_KIND_ORDER_SQL}, episode, shot_id"
                ).bindparams(bindparam("sts", expanding=True)),
                {"t": task_id, "sts": names},
            )
            return [asset_row(r) for r in res.mappings().all()]

    # ── 预算账本（§10.2）──────────────────────────────────
    async def spent_cny(self, task_id: str) -> float:
        """已花费 = Σ cost_cny（含 REJECTED / ORPHANED，钱已花）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text("SELECT COALESCE(SUM(cost_cny),0) FROM asset_state WHERE task_id=:t"), {"t": task_id}
            )
            return round(_to_float(res.scalar()), 3)

    async def reserved_cny(self, task_id: str) -> float:
        """在途预估 = Σ est_cost_cny（PENDING/SUBMITTED/GENERATING/REVIEW，仅当前最大 revision）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT COALESCE(SUM(est_cost_cny),0) FROM asset_state "
                    "WHERE task_id=:t AND status IN :sts "
                    "AND revision = (SELECT COALESCE(MAX(revision),0) FROM asset_state WHERE task_id=:t)"
                ).bindparams(bindparam("sts", expanding=True)),
                {"t": task_id, "sts": list(ASSET_RESERVED_STATUSES)},
            )
            return round(_to_float(res.scalar()), 3)

    async def episode_spent(self, task_id: str) -> dict[str, float]:
        """按集统计已花费。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT episode, COALESCE(SUM(cost_cny),0) AS c FROM asset_state "
                    "WHERE task_id=:t GROUP BY episode"
                ),
                {"t": task_id},
            )
            return {str(r[0]): round(_to_float(r[1]), 3) for r in res.all()}

    async def budget_snapshot(self, task_id: str, cap_cny: float, episode_cap_cny: float) -> BudgetSnapshot:
        """组装预算快照（spent/reserved/单集），供报价与预算闸共用。副作用：读库。"""
        return BudgetSnapshot(
            task_id=task_id,
            spent_cny=await self.spent_cny(task_id),
            reserved_cny=await self.reserved_cny(task_id),
            cap_cny=round(float(cap_cny), 3),
            episode_spent=await self.episode_spent(task_id),
            episode_cap_cny=round(float(episode_cap_cny), 3),
        )

    # ── 报告用聚合 ─────────────────────────────────────────
    async def attempt_hist(self, task_id: str) -> dict[str, int]:
        """attempt 取值直方图（asset_report.attempt_hist）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT attempt, COUNT(*) FROM asset_state WHERE task_id=:t AND kind='video' GROUP BY attempt"
                ),
                {"t": task_id},
            )
            return {str(r[0]): int(r[1]) for r in res.all()}

    async def rejected_videos(self, task_id: str) -> list[AssetRow]:
        """达上限被放弃的视频行（进入 abandoned 清单）。副作用：读库。"""
        return [
            r
            for r in await self.list_by_status(task_id, ["REJECTED"])
            if r.kind == "video"
        ]

    async def counts_by_status(self, task_id: str) -> dict[str, int]:
        """资产状态直方图（派生任务 status 用）。副作用：读库。"""
        async with self.engine.connect() as conn:
            res = await conn.execute(
                text("SELECT status, COUNT(*) FROM asset_state WHERE task_id=:t GROUP BY status"),
                {"t": task_id},
            )
            return {str(r[0]): int(r[1]) for r in res.all()}

    def validate_kind(self, kind: str) -> None:
        """校验资产类型合法。无副作用（只抛异常）。"""
        if kind not in ASSET_KINDS:
            raise IllegalTransition(f"unknown asset kind {kind}")
