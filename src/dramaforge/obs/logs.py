"""职责：loguru 按 stage 分文件（§16）：`route_logs_by_role(role)` 上下文管理器进入时挂 sink、退出时移除。
输入：role/stage 名、task_id、RUNTIME_DIR。
输出：`var/{task_id}/log/{stage}.log` 的写入；控制台 sink 保持不动。

副作用：增删 loguru handler、写日志文件。禁止改变日志级别与全局格式（其它模块依赖）。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from loguru import logger

from ..io import artifacts as io

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {extra[task_id]} {extra[stage]} "
    "{extra[slot]} {extra[revision]} {extra[shot_id]} | {message}"
)
_MISSING = "-"


def _patch_record(record) -> None:
    """给 record.extra 补齐结构字段（缺省为 `-`），保证 format 永不 KeyError。副作用：改 record。"""
    extra = record["extra"]
    for key in ("task_id", "stage", "slot", "revision", "shot_id", "provider", "attempt"):
        extra.setdefault(key, _MISSING)
    extra.setdefault("task_id", _MISSING)
    extra.setdefault("stage", _MISSING)
    extra.setdefault("slot", _MISSING)
    extra.setdefault("revision", _MISSING)
    extra.setdefault("shot_id", _MISSING)


def log_path(task_id: str, stage: str, settings=None) -> Path:
    """`var/{task_id}/log/{stage}.log` 的路径。无副作用。"""
    return io.paths(task_id, settings=settings).log / f"{stage or 'general'}.log"


@contextmanager
def route_logs_by_role(stage: str, task_id: str | None = None, settings=None) -> Iterator[None]:
    """把本阶段日志同时写进 `log/{stage}.log`。副作用：挂/摘 loguru sink。

    task_id 为空时只补字段不落文件（供无任务上下文的调用）。
    """
    if not task_id:
        with logger.contextualize(stage=stage):
            yield
        return
    path = log_path(task_id, stage, settings=settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    sink_id = logger.add(
        str(path),
        level="INFO",
        format=_FILE_FORMAT,
        filter=_patch_record,
        encoding="utf-8",
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )
    try:
        with logger.contextualize(task_id=task_id, stage=stage):
            yield
    finally:
        try:
            logger.remove(sink_id)
        except ValueError:
            pass


def setup_console(level: str = "INFO") -> None:
    """配置控制台 sink（进程启动时调用一次）。副作用：替换 loguru 默认 handler。"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        filter=_patch_record,
        backtrace=False,
        diagnose=False,
    )


def stage_log_files(task_id: str, settings=None) -> list[str]:
    """列出该任务已有的阶段日志文件名（供诊断）。副作用：读目录。"""
    folder = io.paths(task_id, settings=settings).log
    if not folder.exists():
        return []
    return sorted(p.name for p in folder.glob("*.log"))
