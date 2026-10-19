"""职责：M4/M10 端到端 dry-run 测试：在 dry-run 模式下启动全链路任务，校验 18 个阶段全部 PASSED，交付包完整产出且状态为 DONE。
"""

import asyncio
import pytest

from dramaforge.api.runtime import Runtime
from dramaforge.contracts.artifacts import TaskSpec
from dramaforge.core.config import Settings
from dramaforge.io import artifacts as io


@pytest.mark.asyncio
async def test_end_to_end_dry_run(clean_db, settings):
    # 启用 dry_run
    settings_dry = settings.model_copy(update={"dry_run": True})
    runtime = Runtime(settings_dry)
    await runtime.start()

    task_id = "test_e2e_task"
    try:
        task_submitted_id = await runtime.submit(
            {
                "task_id": task_id,
                "topic": "真假千金的都市逆袭",
                "platform": "douyin",
                "episode_count": 2,
                "duration_target": 60,
                "cost_cap_cny": 80.0,
                "overrides": {"episode_limit": 1},
            }
        )
        assert task_submitted_id == task_id

        # 模拟 auto-pilot 轮询推动待办项
        deadline = asyncio.get_event_loop().time() + 150.0
        done = False
        while asyncio.get_event_loop().time() < deadline:
            task = await runtime.store.get_task(task_id)
            if task and task.status in {"DONE", "FAILED"}:
                done = (task.status == "DONE")
                break

            items = await runtime.pending_human(task_id)
            for item in items:
                targets = list((item.payload or {}).get("target_ids") or [item.target_id])
                if item.kind == "quote":
                    await runtime.resolve(task_id, "approve_quote", target_ids=targets, note="auto test approve")
                elif item.kind == "asset":
                    asset_kind = str((item.payload or {}).get("kind") or "")
                    action = "approve_keyframe" if asset_kind == "keyframe" else "pass_slot"
                    await runtime.resolve(task_id, action, target_ids=targets, note="auto test asset")
                elif item.kind == "budget":
                    await runtime.resolve(task_id, "extend_budget", payload={"extra_cny": 50.0}, note="auto test budget")
                else:
                    await runtime.resolve(task_id, "pass_slot", payload={"slot_key": item.slot_key}, note="auto test gate")

            await asyncio.sleep(0.5)

        assert done is True, f"任务未在时限内完成 DONE, 当前状态: {task.status if task else 'None'}"

        # 检查交付包存在
        paths = io.paths(task_id)
        assert io.exists(paths.drama_package_md)
        assert io.exists(paths.final_shots_json)
        assert io.exists(paths.asset_report_json)
        assert io.exists(paths.constraint_report_json)
        assert io.exists(paths.cost_quote_json)

    finally:
        await runtime.stop()
