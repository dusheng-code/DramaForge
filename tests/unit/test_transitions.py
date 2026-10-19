"""职责：M0 验收用例 —— transitions 依赖表、下游计算、状态迁移合法性、派生任务状态（§6.1 / §6.2 / §6.5）。
输入：纯函数与常量。
输出：pytest 断言结果。

副作用：无（不碰 DB、不碰磁盘）。
"""

from __future__ import annotations

import pytest

from dramaforge.contracts.enums import STAGES
from dramaforge.core.transitions import (
    ASSET_STAGES,
    STAGE_DEPS,
    GateViolation,
    IllegalTransition,
    NeedsHuman,
    assert_asset_transition,
    assert_stage_transition,
    dependencies,
    deps_satisfied,
    derive_task_status,
    downstream_of,
    stages_after,
)

# §6.1 依赖表的期望形态（逐条对照文档，防止实现漂移）
EXPECTED_DEPS = {
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


def test_dependency_table_matches_spec():
    """§6.1 依赖表逐条一致，且覆盖全部 14 个阶段。"""
    assert STAGE_DEPS == EXPECTED_DEPS
    assert set(STAGE_DEPS) == set(STAGES)
    assert len(STAGES) == 14


@pytest.mark.parametrize(
    ("stage", "expected_downstream"),
    [
        ("outline", ("episodes", "characters", "blocks", "storyboard", "prompt_pack", "gate", "quote", "anchor_image", "keyframe", "video", "assemble")),
        ("episodes", ("blocks", "storyboard", "prompt_pack", "gate", "quote", "anchor_image", "keyframe", "video", "assemble")),
        ("video", ("assemble",)),
        ("assemble", ()),
        ("reference", tuple(s for s in STAGES if s != "reference")),
    ],
)
def test_downstream_of(stage, expected_downstream):
    """下游关系自底向上闭包正确。"""
    assert downstream_of(stage) == expected_downstream


def test_stages_after_outline_excludes_outline_and_upstream():
    """返工只重写 outline 之后（含 episodes），不重跑大纲与参照。"""
    after = stages_after(outline=True)
    assert "outline" not in after
    assert after[0] == "episodes"
    assert "assemble" in after
    assert "reference" not in after and "constraint" not in after


def test_deps_satisfied_requires_all_gate_slots():
    """quote 依赖 gate 的五个 slot 全 PASSED，缺一不成立。"""
    status_map = {("prompt_pack", "*"): "PASSED"}
    for slot in ("premise", "structure", "emotion", "platform_voice", "risk"):
        status_map[("gate", slot)] = "PASSED"
    assert deps_satisfied("quote", status_map) is True

    status_map[("gate", "risk")] = "NEEDS_HUMAN"
    assert deps_satisfied("quote", status_map) is False


def test_deps_satisfied_multi_dep():
    """blocks 同时依赖 episodes 与 characters。"""
    status_map = {("episodes", "*"): "PASSED", ("characters", "*"): "PENDING"}
    assert deps_satisfied("blocks", status_map) is False
    status_map[("characters", "*")] = "PASSED"
    assert deps_satisfied("blocks", status_map) is True


def test_reference_has_no_dependency():
    """reference 是唯一无依赖阶段。"""
    assert dependencies("reference") == ()
    assert deps_satisfied("reference", {}) is True


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("PENDING", "RUNNING"),
        ("RUNNING", "PASSED"),
        ("RUNNING", "FAILED"),
        ("RUNNING", "NEEDS_HUMAN"),
        ("NEEDS_HUMAN", "PENDING"),
        ("STALE", "PENDING"),
        ("PASSED", "STALE"),
        ("RUNNING", "PENDING"),
    ],
)
def test_legal_stage_transitions(src, dst):
    """§6.2 允许的 stage 迁移不抛异常。"""
    assert_stage_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("PENDING", "PASSED"),
        ("PASSED", "PENDING"),
        ("NEEDS_HUMAN", "PASSED"),
        ("PENDING", "STALE"),
        ("STALE", "PASSED"),
    ],
)
def test_illegal_stage_transitions(src, dst):
    """越界迁移抛 IllegalTransition。"""
    with pytest.raises(IllegalTransition):
        assert_stage_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("PENDING", "SUBMITTED"),
        ("SUBMITTED", "GENERATING"),
        ("GENERATING", "REVIEW"),
        ("REVIEW", "PASSED"),
        ("REVIEW", "REJECTED"),
        ("REJECTED", "PENDING"),
        ("PENDING", "BUDGET_HOLD"),
        ("BUDGET_HOLD", "PENDING"),
        ("GENERATING", "PENDING"),
        ("GENERATING", "ORPHANED"),
        ("PASSED", "ORPHANED"),
    ],
)
def test_legal_asset_transitions(src, dst):
    """§6.2 允许的 asset 迁移。"""
    assert_asset_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        ("PENDING", "PASSED"),
        ("PASSED", "PENDING"),
        ("ORPHANED", "PENDING"),
        ("SUBMITTED", "PASSED"),
    ],
)
def test_illegal_asset_transitions(src, dst):
    """越界 asset 迁移抛 IllegalTransition。"""
    with pytest.raises(IllegalTransition):
        assert_asset_transition(src, dst)


@pytest.mark.parametrize(
    ("stages", "assets", "expected"),
    [
        (["PENDING"], [], "PENDING"),
        (["PASSED", "RUNNING"], [], "RUNNING"),
        ([], ["SUBMITTED"], "RUNNING"),
        (["PASSED"], ["REVIEW"], "AWAITING_HUMAN"),
        (["PASSED", "NEEDS_HUMAN"], [], "NEEDS_HUMAN"),
        (["PASSED"], ["REVIEW", "BUDGET_HOLD"], "BUDGET_HOLD"),
        (["PASSED", "FAILED"], [], "FAILED"),
        (["PASSED", "PASSED"], ["PASSED"], "DONE"),
        ([], [], "PENDING"),
    ],
)
def test_derive_task_status(stages, assets, expected):
    """派生 task.status 的优先级：预算 > 人工 > 运行中 > 等人工 > 失败 > 完成。"""
    assert derive_task_status(stages, assets) == expected


def test_asset_stages_declared():
    """资产阶段集合与 §6.1 一致。"""
    assert set(ASSET_STAGES) == {"anchor_image", "keyframe", "video"}


def test_control_exceptions_carry_payload():
    """GateViolation 携带违规清单、NeedsHuman 携带目标 id，便于上层分流。"""
    exc = GateViolation([{"constraint_id": "c_risk_001"}])
    assert len(exc.violations) == 1
    nh = NeedsHuman("wait", target_ids=["shot-1"])
    assert nh.target_ids == ["shot-1"]
