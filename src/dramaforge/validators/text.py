"""职责：2 个文本证据类确定性校验器（§7）：相似爆款披露、角色语感指纹唯一性。
输入：GateContext（chapters / characters / constraints）。
输出：Violation 列表。

副作用：无。纯字符串与集合判定。
"""

from __future__ import annotations

import re

from ..contracts.artifacts import Violation
from .registry import GateContext, make_violation, register

# 相似爆款披露的关键词（判定「是否声明了与爆款的相似关系」）
SIMILARITY_MARKERS: tuple[str, ...] = ("相似", "爆款", "对标", "借鉴", "同款", "同类型", "复刻")

_PUNCT = re.compile(r"[\s，,。.；;！!？?、：:—\-（）()“”\"'`]")


@register(
    "similarity_disclosure",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="risk",
    description="risk 章节 claims 含 ≥1 条相似爆款且 basis_ids 非空",
)
def similarity_disclosure(ctx: GateContext) -> list[Violation]:
    """risk 段位必须显式披露相似爆款并给出证据（basis_ids 非空）。无副作用。"""
    out: list[Violation] = []
    chapter = next((c for c in ctx.chapters if c.slot_key == "risk"), None)
    disclosed: list[str] = []
    if chapter is not None:
        for claim in chapter.claims:
            text = claim.statement or ""
            if claim.basis_ids and any(m in text for m in SIMILARITY_MARKERS):
                disclosed.append(claim.statement)
    for spec in ctx.constraints_for("similarity_disclosure"):
        if not disclosed:
            out.append(
                make_violation(
                    spec,
                    expected="≥1 条含 basis_ids 的相似爆款披露",
                    actual={"claims": len(chapter.claims) if chapter else 0, "disclosed": 0},
                )
            )
    return out


@register(
    "voice_fingerprint_unique",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="platform_voice",
    description="每角色 ≥1 专属口头禅，且跨角色不重复",
)
def voice_fingerprint_unique(ctx: GateContext) -> list[Violation]:
    """语感指纹：每个角色都要有口头禅，且角色之间不得撞词。无副作用。"""
    out: list[Violation] = []
    for spec in ctx.constraints_for("voice_fingerprint_unique"):
        seen: dict[str, str] = {}
        for char in ctx.characters:
            phrase = _PUNCT.sub("", char.catchphrase or "").strip()
            if not phrase:
                out.append(
                    make_violation(
                        spec,
                        expected="每个角色至少 1 条口头禅",
                        actual={"character_id": char.character_id, "catchphrase": ""},
                    )
                )
                continue
            if phrase in seen and seen[phrase] != char.character_id:
                out.append(
                    make_violation(
                        spec,
                        expected="口头禅跨角色唯一",
                        actual={"catchphrase": phrase, "conflict": [seen[phrase], char.character_id]},
                    )
                )
                continue
            seen[phrase] = char.character_id
    return out
