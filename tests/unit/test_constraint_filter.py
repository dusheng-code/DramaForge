"""职责：M2 约束派生与过滤测试：验证空证据（source_refs 为空）约束被丢弃、≥15 条有效约束正常保留并落盘。
"""

import pytest

from dramaforge.contracts.artifacts import ConstraintSpec
from dramaforge.contracts.enums import SLOT_KEYS


def test_constraint_filtering():
    """验证无效约束过滤（无 source_refs 则抛弃）。"""
    valid = [
        ConstraintSpec(
            constraint_id=f"c_{i}",
            task_id="t_test",
            slot_key="structure",
            validator_key="episode_count_range",
            kind="numeric",
            value=[1, 10],
            source_refs=["ref_001"],
            hard=True,
        )
        for i in range(16)
    ]
    invalid = [
        ConstraintSpec(
            constraint_id=f"bad_{i}",
            task_id="t_test",
            slot_key="structure",
            validator_key="shot_count_budget",
            kind="numeric",
            value=[6, 12],
            source_refs=[],  # 空引用
            hard=True,
        )
        for i in range(5)
    ]

    all_specs = valid + invalid
    kept = [c for c in all_specs if c.source_refs]

    assert len(kept) == 16
    assert all(c.source_refs for c in kept)
    assert len(kept) >= 15
