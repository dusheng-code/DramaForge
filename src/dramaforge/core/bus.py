"""职责：进程内事件总线（只推 SSE，业务分支不得依赖事件到达，§0.7）：订阅、扇出、序号、断线重放。
输入：SystemEvent（seq 由本模块分配）。
输出：每个订阅者一条 asyncio.Queue；订阅者可凭 Last-Event-ID 重放。

副作用：内存队列写入；不做 IO/LLM（§12：回调只 put_nowait）。
"""

from __future__ import annotations

import asyncio
import itertools
from collections import OrderedDict
from datetime import datetime, timezone

from ..contracts.artifacts import SystemEvent


def now_iso() -> str:
    """UTC ISO 时间串（秒级），事件与产物共用。无副作用。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class EventBus:
    """单进程事件总线。每 task 一个有序历史环，供 SSE 断线重连补齐。"""

    def __init__(self, history_size: int = 500) -> None:
        self._seq = itertools.count(1)
        self._subs: dict[str, set[asyncio.Queue[SystemEvent]]] = {}
        self._history: dict[str, OrderedDict[int, SystemEvent]] = {}
        self._history_size = history_size
        self._lock = asyncio.Lock()

    def publish(self, event: SystemEvent) -> SystemEvent:
        """分配序号并扇出到该 task 的全部订阅者。副作用：写内存队列（put_nowait）。"""
        seq = next(self._seq)
        event = event.model_copy(update={"seq": seq, "created_at": event.created_at or now_iso()})
        hist = self._history.setdefault(event.task_id, OrderedDict())
        hist[seq] = event
        while len(hist) > self._history_size:
            hist.popitem(last=False)
        for queue in list(self._subs.get(event.task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue
        return event

    def subscribe(self, task_id: str) -> asyncio.Queue[SystemEvent]:
        """订阅某 task 的事件，返回有界队列（满则丢，前端可用 /stages 拉全量兜底）。无副作用。"""
        queue: asyncio.Queue[SystemEvent] = asyncio.Queue(maxsize=512)
        self._subs.setdefault(task_id, set()).add(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[SystemEvent]) -> None:
        """取消订阅。副作用：移除内存引用。"""
        subs = self._subs.get(task_id)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subs.pop(task_id, None)

    def replay(self, task_id: str, last_event_id: int | None) -> list[SystemEvent]:
        """返回 seq > last_event_id 的历史事件；last_event_id 为空时返回全部。无副作用。"""
        hist = self._history.get(task_id, OrderedDict())
        if last_event_id is None:
            return list(hist.values())
        return [e for e in hist.values() if e.seq > last_event_id]

    def subscriber_count(self, task_id: str) -> int:
        """当前订阅者数量，供测试断言。无副作用。"""
        return len(self._subs.get(task_id, ()))
