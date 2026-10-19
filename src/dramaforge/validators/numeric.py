"""职责：7 个数值/区间类确定性校验器（§7）：集数、单集时长、镜长档位、镜数预算、台词长度与条数、台词与镜长匹配。
输入：GateContext（shots / blocks / constraints / settings）。
输出：Violation 列表（逐约束、逐集或逐镜定位）。

副作用：无。纯确定性判定，不做任何 IO。
"""

from __future__ import annotations

from ..contracts.artifacts import Violation
from .registry import GateContext, make_violation, register, value_as_pair


@register(
    "episode_count_range",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="structure",
    description="集数落在约束区间",
)
def episode_count_range(ctx: GateContext) -> list[Violation]:
    """len(episodes) ∈ value。无副作用。"""
    out: list[Violation] = []
    episodes = ctx.episode_numbers()
    for spec in ctx.constraints_for("episode_count_range"):
        low, high = value_as_pair(spec.value, (1.0, 200.0))
        if not (low <= len(episodes) <= high):
            out.append(make_violation(spec, expected=[low, high], actual=len(episodes)))
    return out


@register(
    "episode_duration_range",
    cost_impact=True,
    judges_self_report=False,
    slot_hint="structure",
    description="每集 Σ shot.duration_sec 落在约束区间",
)
def episode_duration_range(ctx: GateContext) -> list[Violation]:
    """每集总时长 ∈ value；逐集给出 actual 便于定位。无副作用。"""
    out: list[Violation] = []
    by_ep = ctx.shots_by_episode()
    for spec in ctx.constraints_for("episode_duration_range"):
        default = tuple(float(x) for x in ctx.settings.episode_duration_range)
        low, high = value_as_pair(spec.value, default)  # type: ignore[arg-type]
        for ep, shots in by_ep.items():
            total = round(sum(s.duration_sec for s in shots), 3)
            if not (low <= total <= high):
                out.append(
                    make_violation(
                        spec,
                        expected=[low, high],
                        actual=total,
                        shot_ids=[s.shot_id for s in shots],
                    )
                )
    return out


@register(
    "shot_duration_fit_model",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="structure",
    description="每镜 duration_sec ∈ SHOT_DURATION_LADDER",
)
def shot_duration_fit_model(ctx: GateContext) -> list[Violation]:
    """每镜时长必须命中模型档位（默认取配置 SHOT_DURATION_LADDER）。无副作用。"""
    out: list[Violation] = []
    ladder = tuple(ctx.settings.shot_ladder)
    for spec in ctx.constraints_for("shot_duration_fit_model"):
        allowed = tuple(float(x) for x in spec.value) if isinstance(spec.value, (list, tuple)) else ladder
        if not allowed:
            allowed = ladder
        for shot in ctx.shots:
            if not any(abs(shot.duration_sec - a) < 1e-6 for a in allowed):
                out.append(
                    make_violation(spec, expected=list(allowed), actual=shot.duration_sec, shot_ids=[shot.shot_id])
                )
    return out


@register(
    "shot_count_budget",
    cost_impact=True,
    judges_self_report=False,
    slot_hint="structure",
    description="每集镜数 ∈ [MIN_SHOT_PER_EPISODE, MAX_SHOT_PER_EPISODE]",
)
def shot_count_budget(ctx: GateContext) -> list[Violation]:
    """每集镜数落在预算区间（默认取配置）。无副作用。"""
    out: list[Violation] = []
    default = (float(ctx.settings.min_shot_per_episode), float(ctx.settings.max_shot_per_episode))
    for spec in ctx.constraints_for("shot_count_budget"):
        low, high = value_as_pair(spec.value, default)
        for ep, shots in ctx.shots_by_episode().items():
            if not (low <= len(shots) <= high):
                out.append(
                    make_violation(
                        spec,
                        expected=[low, high],
                        actual=len(shots),
                        shot_ids=[s.shot_id for s in shots],
                    )
                )
    return out


@register(
    "line_length_max",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="platform_voice",
    description="单句台词长度 ≤ value",
)
def line_length_max(ctx: GateContext) -> list[Violation]:
    """每句 len(line) ≤ value（中文字符计 1）。无副作用。"""
    out: list[Violation] = []
    for spec in ctx.constraints_for("line_length_max"):
        limit = float(spec.value) if isinstance(spec.value, (int, float)) else 20.0
        for shot in ctx.shots:
            if shot.line and len(shot.line) > limit:
                out.append(
                    make_violation(spec, expected=limit, actual=len(shot.line), shot_ids=[shot.shot_id])
                )
    return out


@register(
    "line_count_range",
    cost_impact=False,
    judges_self_report=False,
    slot_hint="platform_voice",
    description="每集台词句数 ∈ value",
)
def line_count_range(ctx: GateContext) -> list[Violation]:
    """每集非空 line 条数落在区间。无副作用。"""
    out: list[Violation] = []
    default = (1.0, 200.0)
    for spec in ctx.constraints_for("line_count_range"):
        low, high = value_as_pair(spec.value, default)
        for ep, shots in ctx.shots_by_episode().items():
            n = sum(1 for s in shots if s.line)
            if not (low <= n <= high):
                out.append(
                    make_violation(
                        spec, expected=[low, high], actual=n, shot_ids=[s.shot_id for s in shots]
                    )
                )
    return out


@register(
    "line_fits_shot_duration",
    cost_impact=True,
    judges_self_report=False,
    slot_hint="platform_voice",
    description="len(line) ≤ duration_sec × CHARS_PER_SEC",
)
def line_fits_shot_duration(ctx: GateContext) -> list[Violation]:
    """台词必须在镜头时长内读得完（CHARS_PER_SEC 默认取配置）。无副作用。"""
    out: list[Violation] = []
    cps = float(ctx.settings.chars_per_sec or 4.5)
    for spec in ctx.constraints_for("line_fits_shot_duration"):
        rate = float(spec.value) if isinstance(spec.value, (int, float)) else cps
        for shot in ctx.shots:
            if not shot.line:
                continue
            limit = shot.duration_sec * rate
            if len(shot.line) > limit + 1e-6:
                out.append(
                    make_violation(
                        spec,
                        expected=round(limit, 2),
                        actual=len(shot.line),
                        shot_ids=[shot.shot_id],
                    )
                )
    return out
