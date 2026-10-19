"""职责：id 与 spec_version 的确定性生成规则，全系统唯一实现处。
输入：业务字段（project/episode/scene/shot、slot_key、卡片 id、任意内容）。
输出：符合 §5 长度与格式约束的字符串 id。

副作用：无（纯函数）。禁止在别处拼接 shot_id / constraint_id。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

_SLUG_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")


def new_task_id() -> str:
    """生成 32 位十六进制 task_id（对齐 VARCHAR(32) 主键）。无副作用。"""
    return uuid.uuid4().hex


def slug(text: str, limit: int = 24) -> str:
    """把任意文本压成可入 id 的 slug：仅保留中英文数字，其余转下划线。无副作用。"""
    cleaned = _SLUG_RE.sub("_", (text or "").strip()).strip("_")
    return cleaned[:limit] or "x"


def project_key(task_id: str, topic: str = "") -> str:
    """任务级 project 前缀：优先题材 slug，回退 task_id 前 12 位。无副作用。"""
    return slug(topic, limit=16) if topic else task_id[:12]


def shot_id(project: str, episode: int, scene: int, shot: int) -> str:
    """§5.2 shot_id = f"{project}:{ep:02d}:{scene:02d}:{shot:02d}"。无副作用。"""
    return f"{project}:{episode:02d}:{scene:02d}:{shot:02d}"


def block_id(episode: int, seq: int) -> str:
    """剧本 block 的稳定 id：b{ep:02d}{seq:03d}（≤64 字符，可被 shot.source_block_ids 引用）。无副作用。"""
    return f"b{episode:02d}{seq:03d}"


def scene_id(episode: int, scene: int) -> str:
    """场 id：s{ep:02d}{scene:02d}。无副作用。"""
    return f"s{episode:02d}{scene:02d}"


def constraint_id(slot_key: str, seq: int) -> str:
    """§5.2 constraint_id = f"c_{slot_key}_{seq:03d}"。无副作用。"""
    return f"c_{slot_key}_{seq:03d}"


def character_id(display_name: str) -> str:
    """角色 id：c_ + 名字 slug（同批次内由调用方保证去重）。无副作用。"""
    return f"c_{slug(display_name, limit=20)}"


def card_milvus_id(card_id: str, slot_key: str) -> str:
    """§5.3 Milvus 文档 id = f"{card_id}:{slot_key}"，与 MySQL reference_card 1:1 镜像。无副作用。"""
    return f"{card_id}:{slot_key}"


def spec_version(content: Any) -> str:
    """内容版本号 = sha256(canonical_json)[:12]；dict/list 排序键后序列化。无副作用。"""
    if isinstance(content, (dict, list)):
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    elif isinstance(content, bytes):
        raw = content.decode("utf-8", "replace")
    else:
        raw = str(content)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
