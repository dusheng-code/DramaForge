"""职责：JSONL 追加/读取工具（事件流与逐次尝试留痕），路径必须由 io/artifacts.py 提供。
输入：目标 Path、dict/list 行。
输出：追加后的文件；读取得到的行列表。

副作用：追加写磁盘、读磁盘。禁止在别处手写 open(..., "a")。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .artifacts import ensure_dir


def append_lines(path: Path, rows: list[dict[str, Any]]) -> Path:
    """追加多行 JSON。副作用：写磁盘。"""
    ensure_dir(path)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def append_line(path: Path, row: dict[str, Any]) -> Path:
    """追加单行 JSON。副作用：写磁盘。"""
    return append_lines(path, [row])


def read_lines(path: Path) -> list[dict[str, Any]]:
    """读取全部行，跳过损坏行。副作用：读磁盘。"""
    return list(iter_lines(path))


def iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    """逐行产出 dict（生成器，文件缺失时产出空）。副作用：读磁盘。"""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except FileNotFoundError:
        return
