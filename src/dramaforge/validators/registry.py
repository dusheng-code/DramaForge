"""职责：18 个确定性校验器的注册表与 GateContext（§7）：注册、取用、批量执行、机读报告生成。
输入：校验器 key、GateContext。
输出：Violation 列表、ConstraintReport；注册表元数据（self_report / cost_impact）。

副作用：仅写日志。判定必须纯确定性：不得在此发起 LLM / 网络 / 磁盘调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from loguru import logger

from ..contracts.artifacts import (
    Block,
    Character,
    ConstraintReport,
    ConstraintSpec,
    ConstraintVerdict,
    PromptPack,
    Scene,
    SegmentChapter,
    Shot,
    Violation,
)
from ..contracts.enums import SLOT_KEYS, SLOT_LABELS
from ..core.config import Settings, get_settings

Validator = Callable[["GateContext"], list[Violation]]


@dataclass
class GateContext:
    """闸门上下文（§7）：校验器只能读这里的内容，保证判定确定性。"""

    task_id: str
    revision: int = 0
    shots: list[Shot] = field(default_factory=list)
    packs: list[PromptPack] = field(default_factory=list)
    episodes_text: str = ""
    blocks: list[Block] = field(default_factory=list)
    characters: list[Character] = field(default_factory=list)
    chapters: list[SegmentChapter] = field(default_factory=list)
    constraints: list[ConstraintSpec] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    payoff_tags: dict[str, list[str]] = field(default_factory=dict)
    ref_tag_dist: dict[str, float] = field(default_factory=dict)
    settings: Settings = field(default_factory=get_settings)

    # ── 便捷视图 ───────────────────────────────────────────
    def constraints_for(self, validator_key: str, slot_key: str | None = None) -> list[ConstraintSpec]:
        """取出某校验器对应的约束实例（可按 slot 过滤）。无副作用。"""
        return [
            c
            for c in self.constraints
            if c.validator_key == validator_key and (slot_key is None or c.slot_key == slot_key)
        ]

    def shots_by_episode(self) -> dict[int, list[Shot]]:
        """按集分组的镜头（按 scene/shot 原序稳定）。无副作用。"""
        out: dict[int, list[Shot]] = {}
        for s in self.shots:
            out.setdefault(s.episode, []).append(s)
        return dict(sorted(out.items()))

    def packs_by_shot(self) -> dict[str, PromptPack]:
        """shot_id → PromptPack。无副作用。"""
        return {p.shot_id: p for p in self.packs}

    def characters_by_id(self) -> dict[str, Character]:
        """character_id → Character。无副作用。"""
        return {c.character_id: c for c in self.characters}

    def episode_numbers(self) -> list[int]:
        """出现的集号升序（blocks 优先，其次 shots）。无副作用。"""
        eps = sorted({b.episode for b in self.blocks} or {s.episode for s in self.shots})
        return eps


@dataclass(frozen=True)
class ValidatorMeta:
    """校验器元数据（§7 表格的 self_report / cost_impact 两列）。"""

    key: str
    cost_impact: bool = False
    judges_self_report: bool = False
    slot_hint: str = "structure"
    description: str = ""


_REGISTRY: dict[str, Validator] = {}
_META: dict[str, ValidatorMeta] = {}


def register(key: str, *, cost_impact: bool = False, judges_self_report: bool = False,
             slot_hint: str = "structure", description: str = "") -> Callable[[Validator], Validator]:
    """注册校验器（装饰器）。重复注册同一 key 直接报错，避免静默覆盖。无副作用。"""

    def deco(fn: Validator) -> Validator:
        if key in _REGISTRY:
            raise RuntimeError(f"validator already registered: {key}")
        _REGISTRY[key] = fn
        _META[key] = ValidatorMeta(
            key=key,
            cost_impact=cost_impact,
            judges_self_report=judges_self_report,
            slot_hint=slot_hint,
            description=description or (fn.__doc__ or "").strip().splitlines()[0],
        )
        return fn

    return deco


def get(key: str) -> Validator:
    """取校验器；未注册抛 LookupError。无副作用。"""
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise LookupError(f"unknown validator: {key}") from exc


def meta(key: str) -> ValidatorMeta | None:
    """取校验器元数据，未注册返回 None。无副作用。"""
    return _META.get(key)


def all_keys() -> tuple[str, ...]:
    """全部已注册校验器 key（稳定排序）。无副作用。"""
    return tuple(sorted(_REGISTRY))


def cost_impact_keys() -> set[str]:
    """cost_impact=True 的校验器 key 集合（约束装配时强制 needs_human）。无副作用。"""
    return {k for k, m in _META.items() if m.cost_impact}


def self_report_keys() -> set[str]:
    """judges_self_report=True 的校验器 key 集合（报告里标 structural_only）。无副作用。"""
    return {k for k, m in _META.items() if m.judges_self_report}


def run_one(key: str, ctx: GateContext) -> list[Violation]:
    """执行单个校验器；异常不得污染闸门，记 error 后视为「需要人工」（不放行）。副作用：写日志。"""
    try:
        return list(get(key)(ctx) or [])
    except Exception as exc:  # noqa: BLE001 —— 校验器崩了必须显式暴露成 needs_human，不能静默 PASS
        logger.exception("validator crashed: {}", key)
        return [
            Violation(
                constraint_id=f"validator_error_{key}",
                validator_key=key,
                slot_key="structure",
                expected="no error",
                actual=str(exc)[:200],
                severity="hard",
                needs_human=True,
            )
        ]


def run_all(ctx: GateContext) -> list[Violation]:
    """跑全部校验器（每类约束实例各自判定）。副作用：写日志。"""
    keys = {c.validator_key for c in ctx.constraints}
    out: list[Violation] = []
    for key in sorted(keys):
        if key not in _REGISTRY:
            logger.error("constraint references unknown validator: {}", key)
            out.append(
                Violation(
                    constraint_id=f"unknown_validator_{key}",
                    validator_key=key,
                    slot_key="structure",
                    expected="registered validator",
                    actual=key,
                    severity="hard",
                    needs_human=True,
                )
            )
            continue
        out.extend(run_one(key, ctx))
    return out


def build_report(ctx: GateContext, violations: Iterable[Violation]) -> ConstraintReport:
    """把违规清单 + 约束清单合成 constraint_report.json 的机读视图（§8：不经任何 LLM）。无副作用。"""
    by_constraint: dict[str, list[Violation]] = {}
    for v in violations:
        by_constraint.setdefault(v.constraint_id, []).append(v)
    self_report = self_report_keys()
    verdicts: list[ConstraintVerdict] = []
    for spec in ctx.constraints:
        hits = by_constraint.get(spec.constraint_id, [])
        if hits:
            needs_human = any(v.needs_human for v in hits)
            verdicts.append(
                ConstraintVerdict(
                    constraint_id=spec.constraint_id,
                    validator_key=spec.validator_key,
                    slot_key=spec.slot_key,
                    verdict="NEEDS_HUMAN" if needs_human else "FAIL",
                    expected=spec.value,
                    actual=hits[0].actual,
                    shot_ids=sorted({sid for v in hits for sid in v.shot_ids}),
                    severity="hard" if spec.hard else "warn",
                    structural_only=spec.judges_self_report or spec.validator_key in self_report,
                )
            )
        else:
            verdicts.append(
                ConstraintVerdict(
                    constraint_id=spec.constraint_id,
                    validator_key=spec.validator_key,
                    slot_key=spec.slot_key,
                    verdict="PASS",
                    expected=spec.value,
                    actual=None,
                    severity="hard" if spec.hard else "warn",
                    structural_only=spec.judges_self_report or spec.validator_key in self_report,
                )
            )
    hard_failures = sum(1 for v in verdicts if v.verdict == "FAIL" and v.severity == "hard")
    needs_human = sum(1 for v in verdicts if v.verdict == "NEEDS_HUMAN")
    report = ConstraintReport(
        task_id=ctx.task_id,
        revision=ctx.revision,
        verdicts=verdicts,
        passed=hard_failures == 0 and needs_human == 0,
        hard_failures=hard_failures,
        needs_human=needs_human,
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    )
    return report


def violations_of(violations: Iterable[Violation], *, hard_only: bool = False) -> list[Violation]:
    """按 severity 过滤违规。无副作用。"""
    return [v for v in violations if (not hard_only or v.severity == "hard")]


def needs_human_of(violations: Iterable[Violation]) -> list[Violation]:
    """取出需要人工的违规项。无副作用。"""
    return [v for v in violations if v.needs_human]


def slot_label(slot: str) -> str:
    """段位中文名（报告展示用）。无副作用。"""
    return SLOT_LABELS.get(slot, slot)


def slot_order() -> tuple[str, ...]:
    """段位顺序（报告展示用）。无副作用。"""
    return SLOT_KEYS


def make_violation(
    spec: ConstraintSpec,
    *,
    expected=None,
    actual=None,
    shot_ids: list[str] | None = None,
    needs_human: bool | None = None,
) -> Violation:
    """按约束实例构造违规项（severity 由 hard 决定，needs_human 由 fail_action 决定）。

    无副作用。
    """
    if expected is None:
        expected = spec.value
    if needs_human is None:
        needs_human = spec.fail_action == "needs_human"
    return Violation(
        constraint_id=spec.constraint_id,
        validator_key=spec.validator_key,
        slot_key=spec.slot_key,
        expected=expected,
        actual=actual,
        shot_ids=shot_ids or [],
        severity="hard" if spec.hard else "warn",
        needs_human=needs_human,
    )


def value_as_pair(value, default: tuple[float, float]) -> tuple[float, float]:
    """把约束值解析成 (min,max)：支持 [min,max] / {"min":..,"max":..} / 标量。无副作用。"""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            a, b = float(value[0]), float(value[1])
            return (min(a, b), max(a, b))
        except (TypeError, ValueError):
            return default
    if isinstance(value, dict) and "min" in value and "max" in value:
        try:
            a, b = float(value["min"]), float(value["max"])
            return (min(a, b), max(a, b))
        except (TypeError, ValueError):
            return default
    if isinstance(value, (int, float)):
        v = float(value)
        return (v, v)
    return default
