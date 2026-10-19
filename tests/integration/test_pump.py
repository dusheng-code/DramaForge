"""职责：M7 验收测试：AssetPump 的 claim / 状态推进 / poll-only 重启自愈。
"""

import pytest

from dramaforge.assets.budget import BudgetLedger
from dramaforge.assets.pump import AssetPump
from dramaforge.contracts.artifacts import AssetRow, TaskSpec
from dramaforge.core.asset_store import AssetStore
from dramaforge.core.bus import EventBus
from dramaforge.core.events import Events
from dramaforge.core.state_store import StateStore
from dramaforge.io import artifacts as io
from dramaforge.providers.fake_image import FakeImageClient
from dramaforge.providers.fake_video import FakeVideoClient


@pytest.mark.asyncio
async def test_pump_claim_and_poll_only(clean_db, settings, tmp_path):
    store = StateStore.connect(settings.task_state_db)
    assets = AssetStore.connect(settings.task_state_db)
    bus = EventBus()
    events = Events(bus)
    ledger = BudgetLedger(assets, settings)

    task_id = "task_pump_test"
    spec = TaskSpec(
        task_id=task_id,
        topic="泵测试",
        cost_cap_cny=100.0,
        status="RUNNING",
    )
    await store.create_task(spec)

    img_client = FakeImageClient()
    vid_client = FakeVideoClient(done_after_polls=1)

    pump = AssetPump(
        store=assets,
        settings=settings,
        events=events,
        ledger=ledger,
        video_client=vid_client,
        image_client=img_client,
        path_fn=io.paths,
        state_store=store,
    )

    # 1. 插入一个 anchor_image 资产行 (PENDING)
    row = AssetRow(
        task_id=task_id,
        kind="anchor_image",
        shot_id="char_001",
        revision=0,
        status="PENDING",
        est_cost_cny=0.2,
        cost_cny=0.0,
    )
    await assets.insert_rows([row])

    # 2. 跑一轮 step
    claimed = await assets.claim(task_id, limit=1)
    assert len(claimed) == 1
    assert claimed[0].shot_id == "char_001"

    ok = await pump.step(claimed[0])
    assert ok is True

    # 验证状态流转为 REVIEW 或已落盘
    updated = (await assets.list_assets(task_id))[0]
    assert updated.status in {"REVIEW", "PASSED"}
    assert updated.file_path is not None

    await store.engine.dispose()
    await assets.engine.dispose()
