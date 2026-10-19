"""职责：SSE 事件流端点（§12）：按 Last-Event-ID 断线重放 + 长连接扇出，只发事件不查业务表。
输入：task_id、Last-Event-ID 头或查询参数。
输出：text/event-stream（`id:` + `event:` + `data:`）。

副作用：订阅/取消订阅内存队列；不写库。
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..contracts.artifacts import SystemEvent
from .deps import get_runtime, not_found
from .runtime import Runtime

router = APIRouter(prefix="/api/drama/tasks", tags=["events"])

HEARTBEAT_SEC = 15.0


@router.get("/{task_id}/stream")
async def stream_events(
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    since: int | None = Query(default=None, description="等价于 Last-Event-ID 的查询参数"),
    runtime: Runtime = Depends(get_runtime),
) -> StreamingResponse:
    """订阅任务事件流。副作用：占用一条长连接 + 内存队列。"""
    task = await runtime.store.get_task(task_id)
    if task is None:
        raise not_found(task_id)
    cursor = _cursor(last_event_id, since)
    return StreamingResponse(
        _iter(runtime, task_id, cursor, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _cursor(last_event_id: str | None, since: int | None) -> int | None:
    """解析断点：优先头，其次查询参数，都没有则从历史重放。无副作用。"""
    for raw in (last_event_id, since):
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def format_event(event: SystemEvent) -> str:
    """SSE 帧格式化（data 只放事件本体，无正文/密钥）。无副作用。"""
    payload = json.dumps(event.model_dump(), ensure_ascii=False)
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


async def _iter(runtime: Runtime, task_id: str, cursor: int | None, request: Request) -> AsyncIterator[bytes]:
    """先重放历史，再实时扇出；客户端断开即退订。副作用：队列读写。"""
    queue = runtime.bus.subscribe(task_id)
    try:
        for past in runtime.bus.replay(task_id, cursor):
            yield format_event(past).encode("utf-8")
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SEC)
            except asyncio.TimeoutError:
                yield b": heartbeat\n\n"
                continue
            yield format_event(event).encode("utf-8")
    finally:
        runtime.bus.unsubscribe(task_id, queue)
