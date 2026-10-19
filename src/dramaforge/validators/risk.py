"""职责：2 个风险/口味类确定性校验器（§7）：违禁设定命中、爽点密度偏离。
输入：GateContext（episodes_text / shots / payoff_tags / ref_tag_dist / constraints / settings）。
输出：Violation 列表。

副作用：无。payoff_density 属结构闸门（输入含 tagger 自报标签），只 warn 不阻断。
"""

from __future__ import annotations

from ..contracts.artifacts import Violation
from ..contracts.enums import PAYOFF_TAGS
from .registry import GateContext, make_violation, register


@register(
    "forbidden_setting",
    cost_impact=True,
    judges_self_report=False,
    slot_hint="risk",
    description="命中 FORBIDDEN_TERMS 即 hard；未命中仍产 needs_human 项",
)
def forbidden_setting(ctx: GateContext) -> list[Violation]:
    """违禁词扫描（剧本全文 + 镜头 action/line + 提示词）；未命中也要挂一次人工确认。无副作用。"""
    out: list[Violation] = []
    terms = tuple(ctx.settings.forbidden_term_list)
    haystacks: list[tuple[str, str]] = [("episodes", ctx.episodes_text or "")]
    for shot in ctx.shots:
        haystacks.append((shot.shot_id, f"{shot.action or ''}\n{shot.line or ''}"))
    for pack in ctx.packs:
        haystacks.append((f"pack:{pack.shot_id}", pack.image_prompt or ""))
    for spec in ctx.constraints_for("forbidden_setting"):
        spec_terms = tuple(str(t) for t in spec.value) if isinstance(spec.value, (list, tuple)) else terms
        if not spec_terms:
            spec_terms = terms
        hits: dict[str, list[str]] = {}
        for where, text in haystacks:
            for term in spec_terms:
                if term and term in text:
                    hits.setdefault(term, []).append(where)
        if hits:
            out.append(
                make_violation(
                    spec,
                    expected=list(spec_terms),
                    actual={term: where[:5] for term, where in hits.items()},
                    needs_human=False,
                )
            )
        else:
            # 未命中仍产人工项（§7 明确要求）：合规需要人签，机器只负责不漏词
            out.append(
                make_violation(
                    spec,
                    expected=list(spec_terms),
                    actual="no forbidden term matched",
                    needs_human=True,
                )
            )
    return out


@register(
    "payoff_density",
    cost_impact=True,
    judges_self_report=True,
    slot_hint="emotion",
    description="tagger 标签频次分布与参照分布的 L1 距离 ≤ value → warn",
)
def payoff_density(ctx: GateContext) -> list[Violation]:
    """爽点密度：机打标签分布相对参照分布的 L1 距离超阈值时告警（只 warn）。无副作用。"""
    out: list[Violation] = []
    if not ctx.payoff_tags:
        return out
    measured = _distribution(ctx.payoff_tags)
    for spec in ctx.constraints_for("payoff_density"):
        ref = _normalize({k: float(v) for k, v in (ctx.ref_tag_dist or {}).items()})
        if not any(ref.values()):
            continue
        threshold = float(spec.value) if isinstance(spec.value, (int, float)) else 0.5
        distance = _l1(_normalize(measured), ref)
        if distance > threshold:
            out.append(
                make_violation(
                    spec,
                    expected=f"L1 <= {threshold}",
                    actual=round(distance, 4),
                    needs_human=False,
                )
            )
    return out


def _distribution(tags_by_shot: dict[str, list[str]]) -> dict[str, float]:
    """把逐镜标签列表压成频次表（封闭枚举，未知标签忽略）。无副作用。"""
    counts: dict[str, float] = {tag: 0.0 for tag in PAYOFF_TAGS}
    for tags in tags_by_shot.values():
        for tag in tags or []:
            if tag in counts:
                counts[tag] += 1.0
    return counts


def _normalize(dist: dict[str, float]) -> dict[str, float]:
    """归一化成概率分布；全零时返回全零。无副作用。"""
    total = sum(max(0.0, v) for v in dist.values())
    if total <= 0:
        return {tag: 0.0 for tag in PAYOFF_TAGS}
    return {tag: max(0.0, dist.get(tag, 0.0)) / total for tag in PAYOFF_TAGS}


def _l1(p: dict[str, float], q: dict[str, float]) -> float:
    """两个分布之间的 L1 距离。无副作用。"""
    return sum(abs(p.get(tag, 0.0) - q.get(tag, 0.0)) for tag in PAYOFF_TAGS)
