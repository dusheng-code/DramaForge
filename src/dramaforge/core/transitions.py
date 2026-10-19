"""职责：阶段/资产状态机的唯一实现处：依赖表、下游关系、合法迁移判定与三个受控异常。
输入：stage/slot 名称、当前状态、目标状态。
输出：依赖元组、下游阶段集合；非法迁移抛 IllegalTransition。

副作用：无。禁止在别处硬编码阶段依赖或迁移规则（§0.1 / §6.1 / §6.2）。
"""

from __future__ import annotations

from ..contracts.enums import (
    ASSET_TRANSITIONS,
    SLOT_KEYS,
    STAGES,
    STAGE_TRANSITIONS,
    AssetStatus,
    Stage,
    StageStatus,
)


class IllegalTransition(RuntimeError):
    """越界状态迁移（§6.2）。由 state_store / asset_store 抛出。"""


class GateViolation(RuntimeError):
    """gate 汇总出 hard 违规，需要触发 revision+1 返工（§6.4）。"""

    def __init__(self, violations: list, message: str = "") -> None:
        self.violations = violations
        super().__init__(message or f"gate violation: {len(violations)} items")


class NeedsHuman(RuntimeError):
    """需要人工决议才能继续；scheduler 把该行置 NEEDS_HUMAN（§6.3）。"""

    def __init__(self, message: str = "", target_ids: list[str] | None = None) -> None:
        self.target_ids = target_ids or []
        super().__init__(message or "needs human")


# ── §6.1 阶段依赖表（同 revision 需 PASSED）──────────────────
# 值中的 "gate" 特例：表示该 revision 下五个 slot 的 gate 行全部 PASSED。
STAGE_DEPS: dict[str, tuple[str, ...]] = {
    "reference": (),
    "constraint": ("reference",),
    "outline": ("constraint",),
    "episodes": ("outline",),
    "characters": ("outline",),
    "blocks": ("episodes", "characters"),
    "storyboard": ("blocks",),
    "prompt_pack": ("storyboard",),
    "gate": ("prompt_pack",),
    "quote": ("gate",),
    "anchor_image": ("quote",),
    "keyframe": ("anchor_image", "quote"),
    "video": ("keyframe",),
    "assemble": ("video",),
}

# 产物与幂等键（§6.1），state_store 落 output_path 时用
STAGE_IDEMPOTENCY: dict[str, str] = {
    "reference": "task_id",
    "constraint": "task_id",
    "outline": "task_id+rev",
    "episodes": "task_id+rev",
    "characters": "task_id+rev",
    "blocks": "task_id+rev",
    "storyboard": "task_id+rev",
    "prompt_pack": "task_id+rev",
    "gate": "task_id+slot+rev",
    "quote": "task_id+rev",
    "anchor_image": "task_id+cid",
    "keyframe": "task_id+shot+rev",
    "video": "task_id+shot+rev",
    "assemble": "task_id+rev",
}

# 返工时保持 PASSED 复用到新 revision 的阶段（§6.4：只回 episodes，不重跑大纲）
REWORK_PRESERVED: tuple[str, ...] = ("reference", "constraint", "outline")

# 返工的入口阶段与其下游
REWORK_ENTRY = "episodes"

# 资产阶段（这些阶段的 handler 等待 pump 把资产行推到终态）
ASSET_STAGES: tuple[str, ...] = ("anchor_image", "keyframe", "video")

# claim 排序优先级：定妆图 → 首帧 → 视频（§4 链路顺序）
ASSET_KIND_RANK: dict[str, int] = {"anchor_image": 0, "keyframe": 1, "video": 2}


def dependencies(stage: str) -> tuple[str, ...]:
    """返回该阶段的直接依赖阶段。无副作用。"""
    return STAGE_DEPS.get(stage, ())


def downstream_of(stage: str) -> tuple[str, ...]:
    """返回该阶段的所有下游阶段（自底向上计算，含间接下游）。无副作用。"""
    out: set[str] = set()
    frontier = [stage]
    while frontier:
        cur = frontier.pop()
        for cand in STAGES:
            if cur in STAGE_DEPS.get(cand, ()) and cand not in out:
                out.add(cand)
                frontier.append(cand)
    return tuple(s for s in STAGES if s in out)


def stages_after(outline: bool = True) -> tuple[str, ...]:
    """返回 `outline` 之后需要随 revision 重写的阶段集合（§6.4.2）。无副作用。"""
    anchor = "outline" if outline else REWORK_ENTRY
    down = set(downstream_of(anchor))
    if outline:
        return tuple(s for s in STAGES if s in down)
    return tuple(s for s in STAGES if s in down or s == REWORK_ENTRY)


def gate_slots() -> tuple[str, ...]:
    """gate 阶段逐 slot 展开的五个 slot。无副作用。"""
    return SLOT_KEYS


def deps_satisfied(stage: str, status_by_stage_slot: dict[tuple[str, str], str]) -> bool:
    """按依赖表判定依赖是否全部 PASSED（gate 依赖五个 slot 全 PASSED）。无副作用。

    status_by_stage_slot 的键为 (stage, slot_key)，值为状态字符串。
    """
    for dep in dependencies(stage):
        if dep == "gate":
            if any(status_by_stage_slot.get(("gate", s)) != "PASSED" for s in gate_slots()):
                return False
            continue
        if status_by_stage_slot.get((dep, "*")) != "PASSED":
            return False
    return True


def assert_stage_transition(src: str, dst: str) -> None:
    """校验 stage 迁移合法性，非法抛 IllegalTransition。无副作用（只抛异常）。"""
    if src == dst:
        return
    allowed = STAGE_TRANSITIONS.get(src, ())
    if dst not in allowed:
        raise IllegalTransition(f"stage transition {src} -> {dst} not allowed")


def assert_asset_transition(src: str, dst: str) -> None:
    """校验 asset 迁移合法性，非法抛 IllegalTransition。无副作用（只抛异常）。"""
    if src == dst:
        return
    allowed = ASSET_TRANSITIONS.get(src, ())
    if dst not in allowed:
        raise IllegalTransition(f"asset transition {src} -> {dst} not allowed")


def is_terminal_stage(status: str) -> bool:
    """PASSED 视为阶段终态。无副作用。"""
    return status == "PASSED"


def stage_order(stage: str) -> int:
    """阶段在依赖表中的序号，用于排序展示。无副作用。"""
    try:
        return STAGES.index(stage)  # type: ignore[arg-type]
    except ValueError:
        return len(STAGES)


def cast_stage(stage: str) -> Stage:
    """把字符串断言为 Stage（不做运行期校验，供类型检查）。无副作用。"""
    return stage  # type: ignore[return-value]


def cast_stage_status(status: str) -> StageStatus:
    """把字符串断言为 StageStatus。无副作用。"""
    return status  # type: ignore[return-value]


def cast_asset_status(status: str) -> AssetStatus:
    """把字符串断言为 AssetStatus。无副作用。"""
    return status  # type: ignore[return-value]


def derive_task_status(stage_statuses: list[str], asset_statuses: list[str]) -> str:
    """由阶段与资产状态派生 task.status（§6.5「重算派生 status」的唯一实现）。

    优先级：BUDGET_HOLD > NEEDS_HUMAN > RUNNING > AWAITING_HUMAN(等图筛/废片决议)
            > FAILED > DONE > PENDING。无副作用。
    """
    stages = list(stage_statuses or [])
    assets = list(asset_statuses or [])
    if "BUDGET_HOLD" in assets:
        return "BUDGET_HOLD"
    if "NEEDS_HUMAN" in stages:
        return "NEEDS_HUMAN"
    if "RUNNING" in stages or any(a in {"SUBMITTED", "GENERATING"} for a in assets):
        return "RUNNING"
    if "REVIEW" in assets:
        return "AWAITING_HUMAN"
    if "FAILED" in stages:
        return "FAILED"
    if stages and all(s == "PASSED" for s in stages):
        return "DONE"
    return "PENDING"
