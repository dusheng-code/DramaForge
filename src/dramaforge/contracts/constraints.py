"""职责：ConstraintSpec 的装配与过滤规则（无据丢弃、id 归一、cost_impact 强制改写）。
输入：LLM 产出的 ConstraintSpecDraft 列表、合法的证据 id 集合、task_id。
输出：可直接落库的 ConstraintSpec 列表（已按 §6.4.5 / §5.2 规则清洗）。

副作用：仅写日志（warn/info），不读写磁盘与数据库。
"""

from __future__ import annotations

from loguru import logger

from .artifacts import ConstraintSpec, ConstraintSpecDraft
from .enums import SLOT_KEYS, SlotKey
from .ids import constraint_id as make_constraint_id
from .ids import spec_version

# 规格 §7：judges_self_report=True 的校验器（判定输入含模型自报字段）
SELF_REPORT_VALIDATORS: frozenset[str] = frozenset(
    {"first_turn_position", "episode_ending_unresolved", "payoff_density"}
)


def drop_without_evidence(
    drafts: list[ConstraintSpecDraft], valid_refs: set[str] | None = None
) -> tuple[list[ConstraintSpecDraft], list[str]]:
    """丢弃 source_refs 为空的草稿；valid_refs 非空时同时剔除无法解析的引用。

    返回 (保留项, 丢弃原因清单)。无副作用（只记日志）。
    """
    kept: list[ConstraintSpecDraft] = []
    dropped: list[str] = []
    for d in drafts:
        refs = [r for r in d.source_refs if r]
        if valid_refs is not None:
            refs = [r for r in refs if r in valid_refs]
        if not refs:
            dropped.append(f"{d.validator_key}@{d.slot_key}: 无有效 source_refs")
            logger.warning("constraint dropped: no evidence validator={} slot={}", d.validator_key, d.slot_key)
            continue
        d = d.model_copy(update={"source_refs": refs})
        kept.append(d)
    return kept, dropped


def normalize_ids(drafts: list[ConstraintSpecDraft]) -> list[ConstraintSpecDraft]:
    """按 slot 内出现顺序重发 constraint_id，保证 c_{slot}_{seq:03d} 稳定可复现。无副作用。"""
    counters: dict[str, int] = {k: 0 for k in SLOT_KEYS}
    out: list[ConstraintSpecDraft] = []
    for d in drafts:
        counters[d.slot_key] = counters.get(d.slot_key, 0) + 1
        out.append(d.model_copy(update={"constraint_id": make_constraint_id(d.slot_key, counters[d.slot_key])}))
    return out


def enforce_cost_impact(specs: list[ConstraintSpec]) -> list[ConstraintSpec]:
    """§6.4.5：cost_impact=True 禁止 warn_and_continue，强制改写为 needs_human 并记 warn。无副作用。"""
    out: list[ConstraintSpec] = []
    for s in specs:
        if s.cost_impact and s.fail_action == "warn_and_continue":
            logger.warning(
                "cost_impact constraint forced to needs_human: {} slot={}", s.constraint_id, s.slot_key
            )
            s = s.model_copy(update={"fail_action": "needs_human"})
        out.append(s)
    return out


def finalize(
    drafts: list[ConstraintSpecDraft],
    task_id: str,
    valid_refs: set[str] | None = None,
    cost_impact_keys: set[str] | None = None,
) -> tuple[list[ConstraintSpec], list[str]]:
    """一站式装配：过滤无据 → 归一 id → 补 task_id/self_report/cost_impact → 校验器强制改写 → spec_version。

    返回 (specs, 丢弃原因)。cost_impact 由 VALIDATOR_META 推导（见 validators/registry.py），
    因此本函数接受外部传入的 cost_impact_keys 以保持 contracts 层不依赖 validators 层。
    无副作用（只记日志）。
    """
    cost_impact_keys = cost_impact_keys or set()
    kept, dropped = drop_without_evidence(drafts, valid_refs)
    kept = normalize_ids(kept)
    specs: list[ConstraintSpec] = []
    for d in kept:
        spec = ConstraintSpec(
            constraint_id=d.constraint_id or "",
            task_id=task_id,
            slot_key=d.slot_key,
            source_refs=d.source_refs,
            kind=d.kind,
            hard=d.hard,
            value=d.value,
            tolerance=d.tolerance,
            unit=d.unit,
            validator_key=d.validator_key,
            judges_self_report=d.judges_self_report or d.validator_key in SELF_REPORT_VALIDATORS,
            cost_impact=d.validator_key in cost_impact_keys,
            fail_action=d.fail_action,
        )
        if spec.cost_impact and spec.fail_action == "warn_and_continue":
            spec = spec.model_copy(update={"fail_action": "needs_human"})
            logger.warning("cost_impact constraint forced to needs_human: {}", spec.constraint_id)
        spec = spec.model_copy(update={"spec_version": spec_version(_fingerprint(spec))})
        specs.append(spec)
    return specs, dropped


def _fingerprint(spec: ConstraintSpec) -> dict:
    """约束指纹：不含 task_id/spec_version，便于跨任务比对同一约束。无副作用。"""
    data = spec.model_dump()
    data.pop("spec_version", None)
    return data


def by_slot(specs: list[ConstraintSpec]) -> dict[str, list[ConstraintSpec]]:
    """按 slot 分组，缺失 slot 返回空列表占位（保证五份齐全）。无副作用。"""
    out: dict[str, list[ConstraintSpec]] = {k: [] for k in SLOT_KEYS}
    for s in specs:
        out.setdefault(s.slot_key, []).append(s)
    return out


def slot_of(spec: ConstraintSpec) -> SlotKey:
    """透传 slot_key，供 validators 统一取值。无副作用。"""
    return spec.slot_key
