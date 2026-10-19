"""职责：八类事件的构造与发布（§12），把 DB 行的变化翻译成前端可消费的轻量事件。
输入：StageRow / AssetRow / 金额 / 路径等标量。
输出：已发布到 EventBus 的 SystemEvent（payload 只带 id、路径、计数，禁止正文与大字段）。

副作用：写入事件总线内存队列。任何业务分支都不得以“事件已到达”为前提。
"""

from __future__ import annotations

from ..contracts.artifacts import AssetRow, StageRow, SystemEvent
from ..contracts.enums import EventType
from .bus import EventBus, now_iso


class Events:
    """事件发布器：一处收口全部事件类型，避免各处手写字面量。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def emit(self, type_: EventType, task_id: str, **kwargs) -> SystemEvent:
        """发布一条事件。副作用：写事件总线。"""
        allowed = {
            "stage",
            "slot_key",
            "revision",
            "path",
            "data",
        }
        payload = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        payload.setdefault("data", {})
        event = SystemEvent(type=type_, task_id=task_id, created_at=now_iso(), **payload)
        return self.bus.publish(event)

    # ── 八类事件 ───────────────────────────────────────────
    def stage_updated(self, row: StageRow, path: str | None = None) -> SystemEvent:
        """阶段状态变化。副作用：写事件总线。"""
        return self.emit(
            "STAGE_UPDATED",
            row.task_id,
            stage=row.stage,
            slot_key=row.slot_key,
            revision=row.revision,
            path=path,
            data={"status": row.status, "fail_reason": (row.fail_reason or "")[:200]},
        )

    def constraint_ready(self, task_id: str, path: str, count: int, revision: int = 0) -> SystemEvent:
        """约束装配完成（CONSTRAINT_READY）。副作用：写事件总线。"""
        return self.emit(
            "CONSTRAINT_READY",
            task_id,
            stage="constraint",
            revision=revision,
            path=path,
            data={"count": count},
        )

    def progress_message(self, task_id: str, stage: str, message: str, **data) -> SystemEvent:
        """人读进度提示（只放短句，禁止正文）。副作用：写事件总线。"""
        return self.emit(
            "PROGRESS_MESSAGE",
            task_id,
            stage=stage,
            data={"message": message[:240], **data},
        )

    def cost_quote_ready(self, task_id: str, path: str, total_cny: float, revision: int = 0) -> SystemEvent:
        """报价就绪，等待人工 approve_quote。副作用：写事件总线。"""
        return self.emit(
            "COST_QUOTE_READY",
            task_id,
            stage="quote",
            revision=revision,
            path=path,
            data={"est_total_cny": round(float(total_cny), 3)},
        )

    def asset_review_required(self, row: AssetRow, path: str | None = None) -> SystemEvent:
        """资产完成，等人工图筛/视频审核。副作用：写事件总线。"""
        return self.emit(
            "ASSET_REVIEW_REQUIRED",
            task_id=row.task_id,
            path=path,
            data={
                "shot_id": row.shot_id,
                "kind": row.kind,
                "attempt": row.attempt,
                "episode": row.episode,
                "cost_cny": float(row.cost_cny or 0),
            },
        )

    def needs_human(self, task_id: str, stage: str, reason: str, slot_key: str | None = None, path: str | None = None) -> SystemEvent:
        """挂人工。副作用：写事件总线。"""
        return self.emit(
            "NEEDS_HUMAN",
            task_id,
            stage=stage,
            slot_key=slot_key,
            path=path,
            data={"reason": reason[:240]},
        )

    def budget_exceeded(self, task_id: str, spent: float, cap: float, episode: int | None = None) -> SystemEvent:
        """预算触顶（每任务只发一次，由调用方保证）。副作用：写事件总线。"""
        return self.emit(
            "BUDGET_EXCEEDED",
            task_id,
            data={
                "spent_cny": round(float(spent), 3),
                "cap_cny": round(float(cap), 3),
                "episode": episode,
            },
        )

    def task_finished(self, task_id: str, status: str, path: str | None = None, gaps: int = 0) -> SystemEvent:
        """任务终止（DONE/FAILED）。副作用：写事件总线。"""
        return self.emit(
            "TASK_FINISHED",
            task_id,
            stage="assemble",
            path=path,
            data={"status": status, "gaps": gaps},
        )
