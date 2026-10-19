"""职责：任务相关四个端点：提交、列表、详情（含阶段表）、阶段表单独拉取。
输入：SubmitTaskRequest / task_id。
输出：task_id、TaskDetailResponse、阶段视图列表。

副作用：提交会写库并启动协程；读取只读库；均为 HTTP 层，不直接碰 provider。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..core.transitions import STAGE_DEPS
from ..io import artifacts as io
from ..schemas.api import StageView, SubmitTaskRequest, SubmitTaskResponse, TaskDetailResponse, TaskListResponse
from .deps import get_runtime, not_found
from .runtime import Runtime

router = APIRouter(prefix="/api/drama/tasks", tags=["tasks"])


@router.post("", response_model=SubmitTaskResponse)
async def submit_task(req: SubmitTaskRequest, runtime: Runtime = Depends(get_runtime)) -> SubmitTaskResponse:
    """提交新任务（0 号 revision 的 18 行阶段表）。副作用：写库 + 启动调度。"""
    tid = await runtime.submit(req.model_dump())
    stages = await runtime.store.list_stages(tid, 0)
    return SubmitTaskResponse(task_id=tid, status="PENDING", stages=len(stages))


@router.get("", response_model=TaskListResponse)
async def list_tasks(limit: int = 50, runtime: Runtime = Depends(get_runtime)) -> TaskListResponse:
    """任务列表（按创建时间倒序）。无副作用（只读库）。"""
    rows = await runtime.store.list_tasks(limit=min(max(limit, 1), 200))
    return TaskListResponse(tasks=[t.model_dump() for t in rows])


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(task_id: str, runtime: Runtime = Depends(get_runtime)) -> TaskDetailResponse:
    """任务详情 + 当前 revision 的完整阶段表（前端 Progress 页的唯一数据源）。无副作用。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    rev = await runtime.store.current_revision(task_id)
    stages = await runtime.stages_of(task_id)
    pending = await runtime.pending_human(task_id)
    return TaskDetailResponse(
        task=task.model_dump(),
        revision=rev,
        stages=[_view(s) for s in stages],
        pending_human=len(pending),
    )


@router.get("/{task_id}/stages", response_model=list[StageView])
async def get_stages(task_id: str, runtime: Runtime = Depends(get_runtime)) -> list[StageView]:
    """只取阶段表（SSE 丢事件时的兜底拉取）。无副作用。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    return [_view(s) for s in await runtime.stages_of(task_id)]


@router.get("/{task_id}/deps")
async def get_deps(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """阶段依赖表（前端画进度条/依赖检查用）。无副作用。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    return {"deps": {k: sorted(v) for k, v in STAGE_DEPS.items()}}


@router.get("/{task_id}/shots")
async def get_shots(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """分镜 + 首帧（前端 Board 页）：镜头元数据、提示词、首帧文件是否就绪与访问 URL。副作用：读盘。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    paths = runtime.ctx.paths(task_id)
    shots = runtime.ctx.load_shots(task_id)
    packs = {p.shot_id: p for p in runtime.ctx.load_packs(task_id)}
    asset_rows = await runtime.assets.list_assets(task_id)
    keyframes = {r.shot_id: r for r in asset_rows if r.kind == "keyframe"}
    videos = {r.shot_id: r for r in asset_rows if r.kind == "video"}
    out = []
    for shot in shots:
        pack = packs.get(shot.shot_id)
        kf = keyframes.get(shot.shot_id)
        vd = videos.get(shot.shot_id)
        out.append(
            {
                "shot_id": shot.shot_id,
                "episode": shot.episode,
                "scene": shot.scene,
                "duration_sec": shot.duration_sec,
                "camera": shot.camera,
                "action": shot.action,
                "line": shot.line,
                "characters": shot.characters,
                "quality_tier": shot.quality_tier,
                "closing_state": shot.closing_state,
                "is_turn": shot.is_turn,
                "image_prompt": pack.image_prompt if pack else None,
                "motion_prompt": pack.motion_prompt if pack else None,
                "tts_text": pack.tts_text if pack else None,
                "keyframe": _media(keyframes.get(shot.shot_id), paths, "keyframes"),
                "video": _media(videos.get(shot.shot_id), paths, "videos"),
                "status_keyframe": kf.status if kf else "MISSING",
                "status_video": vd.status if vd else "MISSING",
            }
        )
    return {"task_id": task_id, "shots": out}


def _media(row, paths, sub: str) -> dict | None:
    """资产行 → 可访问 URL + 磁盘路径（未落盘返回 None）。副作用：读盘 stat。"""
    if row is None or not row.file_path:
        return None
    target = io.existing_or_none(row.file_path)
    if target is None:
        return None
    name = target.name
    return {"url": f"/var/{paths.task_id}/{sub}/{name}", "path": str(target), "size": target.stat().st_size}


def _view(row) -> StageView:
    """阶段行 → 前端视图（顺带校验产物是否还在）。副作用：读磁盘 stat。"""
    return StageView(
        stage=row.stage,
        slot_key=row.slot_key,
        revision=row.revision,
        status=row.status,
        fail_reason=row.fail_reason,
        output_path=row.output_path,
        output_ok=io.output_ok(row.output_path),
        updated_at=getattr(row, "updated_at", None),
    )
