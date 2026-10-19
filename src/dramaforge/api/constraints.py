"""职责：约束与五段位章节的查询端点（读 constraints.json / constraint_report.json，不重算判定）。
输入：task_id、slot_key。
输出：约束清单 + 机读报告；单段位章节。

副作用：读磁盘。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..contracts.enums import SLOT_KEYS
from ..io import artifacts as io
from ..schemas.api import ConstraintsResponse, SegmentsResponse
from .deps import get_runtime, not_found
from .runtime import Runtime

router = APIRouter(prefix="/api/drama/tasks", tags=["constraints"])


@router.get("/{task_id}/constraints", response_model=ConstraintsResponse)
async def get_constraints(task_id: str, runtime: Runtime = Depends(get_runtime)) -> ConstraintsResponse:
    """约束 + 判定报告（前端 Review 页左栏）。无副作用（只读盘）。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    paths = runtime.ctx.paths(task_id)
    cons = io.read_json(paths.constraints_json, default={}) or {}
    report = io.read_json(paths.constraint_report_json, default={}) or {}
    return ConstraintsResponse(
        task_id=task_id,
        revision=int(cons.get("revision", 0) or 0),
        constraints=list(cons.get("constraints", []) or []),
        report=report,
    )


@router.get("/{task_id}/segments", response_model=list[SegmentsResponse])
async def list_segments(task_id: str, runtime: Runtime = Depends(get_runtime)) -> list[SegmentsResponse]:
    """五个段位章节（五份必须齐全）。无副作用。"""
    return [await get_segment(task_id, slot, runtime) for slot in SLOT_KEYS]


@router.get("/{task_id}/segments/{slot_key}", response_model=SegmentsResponse)
async def get_segment(task_id: str, slot_key: str, runtime: Runtime = Depends(get_runtime)) -> SegmentsResponse:
    """单段位章节。无副作用（只读盘）。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    cons = io.read_json(runtime.ctx.paths(task_id).constraints_json, default={}) or {}
    chapter = next((c for c in cons.get("chapters", []) or [] if c.get("slot_key") == slot_key), None)
    return SegmentsResponse(task_id=task_id, slot_key=slot_key, chapter=chapter, slots=list(SLOT_KEYS))
