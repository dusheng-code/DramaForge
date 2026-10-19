"""职责：五个段位的元数据（标签、说明、章节必填口径）与 slot 相关纯函数。
输入：slot_key 字符串、约束清单。
输出：段位元数据、slot 分类结果、章节完整性判定。

副作用：无。禁止在此读写磁盘或数据库。
"""

from __future__ import annotations

from .enums import SLOT_KEYS, SLOT_LABELS, SlotKey

# 每个 slot 一份 SegmentChapter，五份必须齐全（§5.2）
SLOT_CHAPTER_ROLES: dict[str, str] = {k: "drama" for k in SLOT_KEYS}

# 参照卡/约束在报告中的展示顺序
SLOT_ORDER: dict[str, int] = {k: i for i, k in enumerate(SLOT_KEYS)}


def is_slot(value: str) -> bool:
    """判断字符串是否为合法 slot_key。无副作用。"""
    return value in SLOT_KEYS


def label(slot: str) -> str:
    """返回段位中文名，未知时原样返回。无副作用。"""
    return SLOT_LABELS.get(slot, slot)


def slots_of(items: list[dict]) -> dict[str, int]:
    """按 slot 统计条数，用于缺 slot 检测与报告。无副作用。"""
    out: dict[str, int] = {k: 0 for k in SLOT_KEYS}
    for it in items:
        key = it.get("slot_key")
        if key in out:
            out[key] += 1
    return out


def missing_slots(items: list[dict]) -> list[str]:
    """返回没有任何条目的 slot 列表；空 slot 必须显式记 gaps（§5.2）。无副作用。"""
    counts = slots_of(items)
    return [k for k, v in counts.items() if v == 0]


def order_key(slot: str) -> int:
    """段位排序键，未知段位排最后。无副作用。"""
    return SLOT_ORDER.get(slot, len(SLOT_ORDER))


def normalize(slot: str | None) -> SlotKey | None:
    """把外部传入的 slot 归一化为 SlotKey，非法返回 None。无副作用。"""
    return slot if slot in SLOT_KEYS else None  # type: ignore[return-value]
