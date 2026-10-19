"""职责：资产与预算的查询端点（资产行、报价单、预算快照、asset_report），供前端 Assets 页与人工决策。
输入：task_id。
输出：资产行列表 + 报价 + 预算快照 + 报告。

副作用：读库/读盘；不发起任何 provider 请求。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..assets.quote import read_quote
from ..io import artifacts as io
from ..schemas.api import AssetsResponse
from .deps import get_runtime, not_found
from .runtime import Runtime

router = APIRouter(prefix="/api/drama/tasks", tags=["assets"])


@router.get("/{task_id}/assets", response_model=AssetsResponse)
async def get_assets(task_id: str, runtime: Runtime = Depends(get_runtime)) -> AssetsResponse:
    """资产行 + 报价 + 预算快照（前端 Assets 页）。副作用：读库/读盘。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    paths = runtime.ctx.paths(task_id)
    rows = await runtime.assets.list_assets(task_id)
    quote = read_quote(paths)
    snap = await runtime.ledger.snapshot(task_id, task.cost_cap_cny)
    report = io.read_json(paths.asset_report_json, default=None)
    return AssetsResponse(
        task_id=task_id,
        assets=[r.model_dump() for r in rows],
        quote=quote.model_dump() if quote else None,
        report=report,
        budget=snap.model_dump(),
    )


@router.get("/{task_id}/budget", response_model=dict)
async def get_budget(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """只取预算快照（spent / reserved / cap / 单集）。副作用：读库。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    snap = await runtime.ledger.snapshot(task_id, task.cost_cap_cny)
    per_episode = await runtime.assets.episode_spent(task_id)
    return {"snapshot": snap.model_dump(), "episode_spent": per_episode}


@router.get("/{task_id}/assets/attempts", response_model=dict)
async def get_attempts(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """attempt 直方图与丢弃清单（交付包同源数据）。副作用：读库。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    hist = await runtime.assets.attempt_hist(task_id)
    rejected = await runtime.assets.rejected_videos(task_id)
    return {
        "attempt_hist": hist,
        "rejected": [r.model_dump() for r in rejected],
        "spent_cny": await runtime.assets.spent_cny(task_id),
    }
