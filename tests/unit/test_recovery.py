"""职责：M0 验收用例 —— 启动自愈 recovery（§6.5 / §3.1 产物校验）：RUNNING→PENDING、产物缺失→STALE、资产句柄保留、任务续跑。
输入：真实 MySQL 测试库 + 临时产物目录。
输出：pytest 断言结果。

副作用：读写测试库、在 pytest tmp 目录写产物。
"""

from __future__ import annotations

from functools import partial

import pytest

from dramaforge.contracts.artifacts import AssetRow, TaskSpec
from dramaforge.core.recovery import Recovery
from dramaforge.core.state_store import StateStore
from dramaforge.io import artifacts as io
from tests.fakes import FakeRunner


@pytest.fixture()
def rec(store: StateStore, assets, settings, events, runtime_dir):
    """装配 Recovery：路径注入测试 runtime 目录，runner 用 FakeRunner。副作用：无。"""
    runner = FakeRunner()
    path_fn = partial(io.paths, settings=settings)
    manager = Recovery(store=store, assets=assets, settings=settings, events=events, runner=runner, path_fn=path_fn)
    return manager, runner, path_fn


async def _new_task(store: StateStore, task_id: str = "t-recovery") -> TaskSpec:
    """建一个 revision 0 全 PENDING 的任务。副作用：写库。"""
    spec = TaskSpec(task_id=task_id, topic="外卖员逆袭", episode_count=2, cost_cap_cny=100.0)
    await store.create_task(spec)
    return spec


async def test_running_stage_reset_to_pending(store: StateStore, rec):
    """RUNNING → PENDING（§6.5 第一条）。"""
    manager, runner, _ = rec
    await _new_task(store)
    row = await store.get_stage("t-recovery", "reference")
    assert row is not None
    assert await store.cas(row, "PENDING", "RUNNING")

    out = await manager.recover_task("t-recovery")

    row = await store.get_stage("t-recovery", "reference")
    assert row.status == "PENDING"
    assert out["reset_stage"] == 1
    assert runner.submitted == ["t-recovery"]


async def test_passed_stage_with_missing_output_becomes_stale(store: StateStore, rec, task_root):
    """产物丢失 → STALE 重跑，不信任状态表（§3.1）。"""
    manager, runner, path_fn = rec
    await _new_task(store)
    paths = path_fn("t-recovery").ensure()

    existing = io.write_text(paths.outline_md, "# 大纲")
    row = await store.get_stage("t-recovery", "outline")
    assert await store.cas(row, "PENDING", "RUNNING")
    assert await store.cas(row, "RUNNING", "PASSED", output_path=str(existing))

    gone = paths.characters_json
    row = await store.get_stage("t-recovery", "characters")
    assert await store.cas(row, "PENDING", "RUNNING")
    assert await store.cas(row, "RUNNING", "PASSED", output_path=str(gone))

    out = await manager.recover_task("t-recovery")

    assert (await store.get_stage("t-recovery", "outline")).status == "PASSED"
    stale = await store.get_stage("t-recovery", "characters")
    assert stale.status == "STALE"
    assert stale.fail_reason == "output missing"
    assert out["stale_stage"] == 1


async def test_assets_keep_handles_and_review_untouched(store: StateStore, assets, settings, rec):
    """SUBMITTED/GENERATING 保留 provider_task_id 续轮询；REVIEW 不动；PENDING 不动（§6.5）。"""
    manager, runner, _ = rec
    await _new_task(store)
    rows = [
        AssetRow(task_id="t-recovery", shot_id="p:01:01:01", kind="keyframe", episode=1,
                 status="SUBMITTED", attempt=1, provider_task_id="prov-1"),
        AssetRow(task_id="t-recovery", shot_id="p:01:01:02", kind="keyframe", episode=1,
                 status="GENERATING", attempt=1, provider_task_id="prov-2"),
        AssetRow(task_id="t-recovery", shot_id="p:02:01:01", kind="keyframe", episode=2,
                 status="REVIEW", attempt=0, file_path="/tmp/x.png"),
        AssetRow(task_id="t-recovery", shot_id="p:02:01:02", kind="video", episode=2, status="PENDING"),
    ]
    await assets.insert_rows(rows)

    await manager.recover_task("t-recovery")

    got = {r.shot_id: r for r in await assets.list_assets("t-recovery")}
    assert got["p:01:01:01"].status == "SUBMITTED"
    assert got["p:01:01:01"].provider_task_id == "prov-1"
    assert got["p:01:01:02"].status == "GENERATING"
    assert got["p:01:01:02"].provider_task_id == "prov-2"
    assert got["p:02:01:01"].status == "REVIEW"
    assert got["p:02:01:02"].status == "PENDING"


async def test_recovery_writes_snapshots(store: StateStore, rec):
    """恢复后写 state/stage_snapshot.json 与 asset_snapshot.json（§15）。"""
    manager, runner, path_fn = rec
    await _new_task(store)
    await manager.recover_task("t-recovery")
    paths = path_fn("t-recovery")
    snap = io.read_json(paths.stage_snapshot_json)
    assert snap["task_id"] == "t-recovery"
    assert len(snap["stages"]) == 18  # 13 全局阶段 + gate×5
    assert io.read_json(paths.asset_snapshot_json)["assets"] == []


async def test_finished_task_not_resubmitted(store: StateStore, rec):
    """终态任务不再交回 submit_task（§6.5）。"""
    manager, runner, _ = rec
    await _new_task(store)
    for row in await store.list_stages("t-recovery"):
        assert await store.cas(row, "PENDING", "RUNNING")
        assert await store.cas(row, "RUNNING", "PASSED")
    await store.update_task_status("t-recovery", "DONE")

    out = await manager.recover_task("t-recovery")

    assert out["resubmitted"] is False
    assert runner.submitted == []
    assert (await store.get_task("t-recovery")).status == "DONE"


async def test_recover_all_scans_active_tasks(store: StateStore, rec):
    """recover_all 只扫非终态任务并逐任务自愈。"""
    manager, runner, _ = rec
    await _new_task(store, "t-a")
    await _new_task(store, "t-b")
    await store.update_task_status("t-b", "DONE")

    stats = await manager.run()

    assert stats["tasks"] == 1
    assert stats["resubmitted"] == 1
    assert runner.submitted == ["t-a"]
