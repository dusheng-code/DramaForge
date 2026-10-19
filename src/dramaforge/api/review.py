"""职责：人工决议回路（§8 Human Resolver）：待人工列表 + 执行决议（报价批准/追加预算/首帧与视频审核/段位放行/改约束/强制返工）。
输入：task_id、ResolveRequest。
输出：决议结果与留痕；随后自动重启该任务调度。

副作用：写库（resolution/stage/asset）、写盘（pending_human.json、constraints.json）、创建协程。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..io import artifacts as io
from ..schemas.api import PendingHumanResponse, ResolveRequest, ResolveResponse
from .deps import get_runtime, not_found
from .runtime import Runtime

router = APIRouter(prefix="/api/drama/tasks", tags=["review"])


@router.get("/{task_id}/pending-human", response_model=PendingHumanResponse)
async def pending_human(task_id: str, runtime: Runtime = Depends(get_runtime)) -> PendingHumanResponse:
    """待人工项（阶段行 + 资产审核 + 预算触顶）。副作用：读库 + 写 pending_human.json。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    items = await runtime.pending_human(task_id)
    io.write_json(runtime.ctx.paths(task_id).pending_human_json, {"task_id": task_id, "items": [i.model_dump() for i in items]})
    return PendingHumanResponse(task_id=task_id, items=[i.model_dump() for i in items])


@router.post("/{task_id}/resolve", response_model=ResolveResponse)
async def resolve(task_id: str, req: ResolveRequest, runtime: Runtime = Depends(get_runtime)) -> ResolveResponse:
    """执行一次人工决议。副作用：写库/写盘 + 重启调度。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    try:
        result = await runtime.resolve(
            task_id,
            action=str(req.action),
            target_ids=req.target_ids,
            payload=req.payload,
            note=req.note,
            operator=req.operator,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResolveResponse(
        task_id=task_id,
        action=str(req.action),
        revision=int(result.get("revision", 0) or 0),
        detail=result.get("detail") or {},
    )


@router.get("/{task_id}/resolutions", response_model=dict)
async def resolutions(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """人工决议历史（审计）。副作用：读库。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    return {"task_id": task_id, "items": await runtime.store.list_resolutions(task_id)}
