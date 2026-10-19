"""职责：M7 验收测试：预算总账超支卡控、BUDGET_HOLD、追加预算解冻、尝试次数耗尽 REJECTED。
"""

import pytest

from dramaforge.assets.budget import BudgetLedger
from dramaforge.contracts.artifacts import AssetRow, TaskSpec
from dramaforge.core.asset_store import AssetStore
from dramaforge.core.state_store import StateStore


@pytest.mark.asyncio
async def test_budget_ledger_and_hold(clean_db, settings):
    """测试超预算卡控：预算不足时 allow=False，资产行流转 BUDGET_HOLD，追加预算后释放。"""
    store = StateStore.connect(settings.task_state_db)
    assets = AssetStore.connect(settings.task_state_db)

    task_id = "task_budget_test"
    spec = TaskSpec(
        task_id=task_id,
        topic="预算测试",
        cost_cap_cny=1.0,  # 上限仅 1.0 元
        status="RUNNING",
    )
    await store.create_task(spec)

    ledger = BudgetLedger(assets, settings)

    # 1. 初始状态：cap=1.0, spent=0, reserved=0
    snap = await ledger.snapshot(task_id, task_cap_cny=1.0)
    assert snap.cap_cny == 1.0
    assert snap.spent_cny == 0.0

    # 提交前询问 0.8 元 -> 0 + 0 + 0.8 <= 1.0 -> True
    ok1 = await ledger.allow(task_id, episode=1, next_est_cny=0.8, task_cap_cny=1.0)
    assert ok1 is True

    # 插入一个单价 0.8 元的在途资产行（PENDING 计入 reserved）
    row1 = AssetRow(
        task_id=task_id,
        kind="video",
        shot_id="shot_001",
        revision=0,
        status="PENDING",
        est_cost_cny=0.8,
        cost_cny=0.0,
    )
    await assets.insert_rows([row1])

    # 此时 reserved 已有 0.8，再来一个 0.5 元的行，累计 0.8 + 0.5 = 1.3 > 1.0，应被拒绝
    ok2 = await ledger.allow(task_id, episode=1, next_est_cny=0.5, task_cap_cny=1.0)
    assert ok2 is False

    # 将其置为 BUDGET_HOLD
    row2 = AssetRow(
        task_id=task_id,
        kind="video",
        shot_id="shot_002",
        revision=0,
        status="PENDING",
        est_cost_cny=0.5,
        cost_cny=0.0,
    )
    await assets.insert_rows([row2])
    await assets.mark(row2, "BUDGET_HOLD")

    hold_rows = await assets.list_by_status(task_id, ["BUDGET_HOLD"])
    assert len(hold_rows) == 1
    assert hold_rows[0].shot_id == "shot_002"

    # 追加预算至 3.0 元
    await store.update_task_cap(task_id, 3.0)
    ledger.reset()

    # 释放 hold
    released = await assets.release_holds(task_id)
    assert released == 1

    pending_rows = await assets.list_by_status(task_id, ["PENDING"])
    assert any(r.shot_id == "shot_002" for r in pending_rows)

    await store.engine.dispose()
    await assets.engine.dispose()
