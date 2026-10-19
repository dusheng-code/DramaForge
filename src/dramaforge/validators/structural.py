"""职责：7 个结构类确定性校验器（§7）：动作复杂度、角色存在性、角色一致性、提示词完整、服化道连续性、首次反转位、集末悬念。
输入：GateContext（shots / packs / characters / constraints）。
输出：Violation 列表（尽量定位到 shot_id）。

副作用：无。纯确定性判定；动作复杂度只做「分句 + 词表命中计数」，**不做语义解析**（§7 补充口径）。
"""

from __future__ import annotations

import re

from ..contracts.artifacts import Violation
from ..contracts.enums import PROMPT_MARKERS
from .registry import GateContext, make_violation, register, value_as_pair

_CLAUSE_SPLIT = re.compile(r"[，,。.；;！!？?\s]+")
DEFAULT_VERB_CAP = 2
DEFAULT_CHAR_CAP = 1


@register(
    "action_complexity_cap",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="emotion",
    description="action 中出现的角色数 ≤1 且分句后动词数 ≤2",
)
def action_complexity_cap(ctx: GateContext) -> list[Violation]:
    """单镜动作复杂度上限：角色数 ≤1、动词数 ≤2（词表命中计数）。无副作用。"""
    out: list[Violation] = []
    verbs = tuple(ctx.settings.action_verb_list)
    char_names: dict[str, list[str]] = {}
    for c in ctx.characters:
        names = [c.character_id, c.display_name]
        char_names[c.character_id] = [n for n in names if n]
    for spec in ctx.constraints_for("action_complexity_cap"):
        value = spec.value if isinstance(spec.value, dict) else {}
        char_cap = int(value.get("characters") or DEFAULT_CHAR_CAP)
        verb_cap = int(value.get("verbs") or DEFAULT_VERB_CAP)
        for shot in ctx.shots:
            hits = 0
            for cid in shot.characters:
                if cid in char_names:
                    if any(name in shot.action for name in char_names[cid]):
                        hits += 1
            if len(shot.characters) > char_cap:
                out.append(
                    make_violation(
                        spec,
                        expected={"characters": char_cap},
                        actual={"characters": len(shot.characters)},
                        shot_ids=[shot.shot_id],
                    )
                )
                continue
            if hits > char_cap:
                out.append(
                    make_violation(
                        spec,
                        expected={"appearing_characters": char_cap},
                        actual={"appearing_characters": hits},
                        shot_ids=[shot.shot_id],
                    )
                )
                continue
            n_verbs = 0
            for clause in _CLAUSE_SPLIT.split(shot.action or ""):
                if not clause:
                    continue
                n_verbs += sum(1 for v in verbs if v and v in clause)
            if n_verbs > verb_cap:
                out.append(
                    make_violation(
                        spec,
                        expected={"verbs": verb_cap},
                        actual={"verbs": n_verbs},
                        shot_ids=[shot.shot_id],
                    )
                )
    return out


@register(
    "characters_exist",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="premise",
    description="shot.characters ⊆ characters.json 的 id 集合",
)
def characters_exist(ctx: GateContext) -> list[Violation]:
    """镜头里出现的角色必须都在 characters.json 里登记。无副作用。"""
    out: list[Violation] = []
    known = set(ctx.characters_by_id())
    for spec in ctx.constraints_for("characters_exist"):
        for shot in ctx.shots:
            unknown = [c for c in shot.characters if c not in known]
            if unknown:
                out.append(
                    make_violation(spec, expected=sorted(known), actual=unknown, shot_ids=[shot.shot_id])
                )
    return out


@register(
    "character_consistency",
    cost_impact=True,
    judges_self_report=False,
    slot_hint="premise",
    description="出场角色的 anchor_text 必须是其每镜 image_prompt 的子串",
)
def character_consistency(ctx: GateContext) -> list[Violation]:
    """视觉锚强一致性：anchor_text 必须是 image_prompt 子串，否则首帧会换脸。无副作用。"""
    out: list[Violation] = []
    packs = ctx.packs_by_shot()
    chars = ctx.characters_by_id()
    for spec in ctx.constraints_for("character_consistency"):
        for shot in ctx.shots:
            pack = packs.get(shot.shot_id)
            if pack is None:
                out.append(
                    make_violation(spec, expected="pack exists", actual=None, shot_ids=[shot.shot_id])
                )
                continue
            for cid in shot.characters:
                char = chars.get(cid)
                if char is None:
                    continue
                anchor = char.anchor_for_episode(shot.episode)
                if anchor and anchor not in pack.image_prompt:
                    out.append(
                        make_violation(
                            spec,
                            expected=anchor,
                            actual=pack.image_prompt[:160],
                            shot_ids=[shot.shot_id],
                        )
                    )
    return out


@register(
    "prompt_pack_complete",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="platform_voice",
    description="image_prompt 必须含 [CHAR][SCENE][CAMERA][ACTION][LIGHT] 五标记",
)
def prompt_pack_complete(ctx: GateContext) -> list[Violation]:
    """五要素标记齐全（§9.1 模板）。无副作用。"""
    out: list[Violation] = []
    for spec in ctx.constraints_for("prompt_pack_complete"):
        for pack in ctx.packs:
            missing = [m for m in PROMPT_MARKERS if m not in pack.image_prompt]
            if missing:
                out.append(
                    make_violation(spec, expected=list(PROMPT_MARKERS), actual=missing, shot_ids=[pack.shot_id])
                )
    return out


@register(
    "wardrobe_continuity",
    cost_impact=True,
    judges_self_report=False,
    slot_hint="structure",
    description="ep_start ≥ episode 时命中替换锚，否则用主锚",
)
def wardrobe_continuity(ctx: GateContext) -> list[Violation]:
    """服化道连续：换装后必须用替换锚，换装前不得用替换锚。无副作用。"""
    out: list[Violation] = []
    packs = ctx.packs_by_shot()
    for spec in ctx.constraints_for("wardrobe_continuity"):
        for shot in ctx.shots:
            pack = packs.get(shot.shot_id)
            if pack is None:
                continue
            for cid in shot.characters:
                char = ctx.characters_by_id().get(cid)
                if char is None:
                    continue
                changes = char.visual_anchor.wardrobe_change or []
                if not changes:
                    continue
                applicable = []
                for item in changes:
                    try:
                        if int(item.get("ep_start", 0)) <= shot.episode:
                            applicable.append(str(item.get("anchor_text") or ""))
                    except (TypeError, ValueError):
                        continue
                if applicable:
                    anchor = applicable[-1]
                    if anchor and anchor not in pack.image_prompt:
                        out.append(
                            make_violation(
                                spec,
                                expected=anchor,
                                actual=pack.image_prompt[:160],
                                shot_ids=[shot.shot_id],
                            )
                        )
                else:
                    main = char.visual_anchor.anchor_text
                    if main and main not in pack.image_prompt:
                        out.append(
                            make_violation(
                                spec, expected=main, actual=pack.image_prompt[:160], shot_ids=[shot.shot_id]
                            )
                        )
    return out


@register(
    "first_turn_position",
    cost_impact=True,
    judges_self_report=True,
    slot_hint="structure",
    description="首个 is_turn=True 的集号 ≤ value",
)
def first_turn_position(ctx: GateContext) -> list[Violation]:
    """第一次反转/亮底牌必须早于约束集号（输入含模型自报字段，属结构闸门）。无副作用。"""
    out: list[Violation] = []
    turns = sorted({s.episode for s in ctx.shots if s.is_turn})
    first = turns[0] if turns else None
    for spec in ctx.constraints_for("first_turn_position"):
        low, high = value_as_pair(spec.value, (1.0, float(ctx.settings.topic_default_episodes)))
        if first is None or first > high:
            out.append(make_violation(spec, expected=f"<= {high}", actual=first))
    return out


@register(
    "episode_ending_unresolved",
    cost_impact=True,
    judges_self_report=True,
    slot_hint="emotion",
    description="每集末镜 closing_state != resolved",
)
def episode_ending_unresolved(ctx: GateContext) -> list[Violation]:
    """每集必须留钩子：末镜 closing_state 不得为 resolved。无副作用。"""
    out: list[Violation] = []
    for spec in ctx.constraints_for("episode_ending_unresolved"):
        for ep, shots in ctx.shots_by_episode().items():
            last = shots[-1]
            if last.closing_state == "resolved":
                out.append(
                    make_violation(
                        spec,
                        expected="cliffhanger_line|unresolved_action|reveal_tease",
                        actual="resolved",
                        shot_ids=[last.shot_id],
                    )
                )
    return out
