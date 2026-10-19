"""职责：FastAPI 依赖注入的取用点（从 app.state 取 Runtime；避免模块级全局单例）。
输入：Request。
输出：Runtime 实例。

副作用：无。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from .runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    """取当前进程的 Runtime。无副作用。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime not ready")
    return runtime


def not_found(task_id: str) -> HTTPException:
    """统一的 404。无副作用。"""
    return HTTPException(status_code=404, detail=f"task not found: {task_id}")
