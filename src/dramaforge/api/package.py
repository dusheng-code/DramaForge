"""职责：交付包查询端点（drama_package.md / final_shots.json），支持 `?as=md|json` 与原始文件下载。
输入：task_id、as。
输出：交付包内容或文件响应。

副作用：读磁盘。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from ..io import artifacts as io
from .deps import get_runtime, not_found
from .runtime import Runtime

router = APIRouter(prefix="/api/drama/tasks", tags=["package"])


@router.get("/{task_id}/package")
async def get_package(
    task_id: str,
    as_: str = Query(default="md", alias="as", pattern="^(md|json|report)$"),
    runtime: Runtime = Depends(get_runtime),
):
    """交付包：md 返回文本，json 返回 final/shots.json，report 返回 final/asset_report.json。副作用：读盘。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    paths = runtime.ctx.paths(task_id)
    if as_ == "md":
        if not io.exists(paths.drama_package_md):
            raise HTTPException(status_code=404, detail="drama_package.md not ready")
        return PlainTextResponse(io.read_text(paths.drama_package_md), media_type="text/markdown; charset=utf-8")
    target = paths.final_shots_json if as_ == "json" else paths.asset_report_json
    if not io.exists(target):
        raise HTTPException(status_code=404, detail=f"{target.name} not ready")
    return FileResponse(target, media_type="application/json", filename=f"{task_id}_{target.name}")


@router.get("/{task_id}/package/files", response_model=dict)
async def list_package_files(task_id: str, runtime: Runtime = Depends(get_runtime)) -> dict:
    """交付目录文件清单（含大小，供前端下载区）。副作用：读磁盘。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    paths = runtime.ctx.paths(task_id)
    files = []
    for name in ("drama_package.md", "shots.json", "asset_report.json"):
        target = paths.final / name
        files.append(
            {
                "name": name,
                "path": str(target),
                "exists": io.exists(target),
                "size": target.stat().st_size if target.exists() else 0,
            }
        )
    return {"task_id": task_id, "files": files}
