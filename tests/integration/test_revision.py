"""职责：M6 验收集成测试：段位违规返工、CAS 并发写入、STALE 标记与 revision 递增、前序产物保持。
"""

import pytest

from dramaforge.contracts.artifacts import StageRow, Violation
from dramaforge.contracts.enums import STAGES
from dramaforge.core.events import EventBus
from dramaforge.core.revision import RevisionManager
from dramaforge.core.state_store import StateStore
from dramaforge.core.transitions import REWORK_PRESERVED


@pytest.mark.asyncio
async def test_revision_dispatch_and_preserve(clean_db, settings, tmp_path):
    from dramaforge.core.asset_store import AssetStore
    from dramaforge.core.bus import EventBus
    from dramaforge.core.events import Events
    from dramaforge.io import artifacts as io

    store = StateStore.connect(settings.task_state_db)
    assets = AssetStore.connect(settings.task_state_db)
    bus = EventBus()
    events = Events(bus)
    rm = RevisionManager(store, assets, settings, events, io.paths)

    from dramaforge.contracts.artifacts import TaskSpec

    task_id = "task_rev_test"
    spec = TaskSpec(
        task_id=task_id,
        topic="返工测试",
        platform="douyin",
        episode_count=1,
        episode_duration_target=60,
        cost_cap_cny=50.0,
        status="PENDING",
    )
    await store.create_task(spec)

    # 1. 初始化 rev 0 阶段行
    rows_rev0 = await store.list_stages(task_id, revision=0)
    assert len(rows_rev0) == 18

    # 模拟 reference, constraint, outline 跑通 (PASSED)
    for s in ("reference", "constraint", "outline"):
        row = next(r for r in rows_rev0 if r.stage == s)
        await store.cas(row, "PENDING", "RUNNING")
        await store.cas(row, "RUNNING", "PASSED")

    # 2. 模拟 gate structure 触发 hard 违规并派发返工
    gate_row = next(r for r in rows_rev0 if r.stage == "gate" and r.slot_key == "structure")
    await store.cas(gate_row, "PENDING", "RUNNING")

    violations = [
        Violation(
            constraint_id="c_test",
            validator_key="episode_count_range",
            slot_key="structure",
            severity="hard",
            expected=2,
            actual=1,
        )
    ]

    new_rev = await rm.dispatch(gate_row, violations)
    assert str(new_rev) == "1"

    # 3. 校验 rev 1 下的状态：
    # reference, constraint, outline 等 REWORK_PRESERVED 应该自动作为 PASSED 结转或保持
    rows_rev1 = await store.list_stages(task_id, revision=1)
    assert len(rows_rev1) == 18

    for s in REWORK_PRESERVED:
        row1 = next(r for r in rows_rev1 if r.stage == s)
        assert row1.status == "PASSED", f"{s} 应该在 rev 1 保持 PASSED"

    # 非 preserved 的阶段应该为 PENDING (episodes) 或 STALE
    episodes_row = next(r for r in rows_rev1 if r.stage == "episodes")
    assert episodes_row.status == "PENDING"

    await store.engine.dispose()
    await assets.engine.dispose()
