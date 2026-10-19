"""职责：一条命令跑通全链路的演示/验收脚本（`--dry-run` 零花费、`--auto` 自动代打人工闸），并在结束时打印产物与花费摘要。
输入：命令行参数（--topic/--episodes/--budget/--dry-run/--auto/--timeout/--keep-db-task）。
输出：控制台进度 + 交付包路径 + 阶段表 + 花费统计（退出码：0 完成 / 2 超时 / 3 失败）。

副作用：写库、写磁盘（var/tasks/<task_id>/）、调用 LLM（dry-run 走 FakeLLMClient）、dry-run 下走 Fake provider。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from loguru import logger

from ..api.runtime import Runtime
from ..core.config import Settings, get_settings


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。无副作用。"""
    p = argparse.ArgumentParser(prog="dramaforge-demo", description="DramaForge 端到端演示")
    p.add_argument("--topic", default="退婚后我继承了前夫的公司")
    p.add_argument("--platform", default="douyin")
    p.add_argument("--episodes", type=int, default=8)
    p.add_argument("--duration", type=int, default=75, help="单集目标时长(秒)")
    p.add_argument("--budget", type=float, default=120.0, help="预算上限(元)")
    p.add_argument("--dry-run", action="store_true", help="零花费：FakeLLM + Fake provider")
    p.add_argument("--auto", action="store_true", help="自动代打人工闸（报价批准/段位放行）")
    p.add_argument("--episode-limit", type=int, default=0, help="只跑前 N 集（M4 用）")
    p.add_argument("--timeout", type=float, default=900.0, help="整体超时(秒)")
    p.add_argument("--task-id", default=None)
    return p.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    """按参数覆盖 Settings（不写 .env）。无副作用。"""
    base = get_settings()
    update: dict[str, Any] = {
        "dry_run": bool(args.dry_run) or base.dry_run,
        "episode_limit": int(args.episode_limit),
        "hero_max_ratio": base.hero_max_ratio,
    }
    return base.model_copy(update=update)


async def _auto_pilot(runtime: Runtime, task_id: str, deadline: float) -> str:
    """自动代打人工闸直到任务终态。副作用：写库/写盘 + 重启调度。"""
    while time.monotonic() < deadline:
        task = await runtime.store.get_task(task_id)
        if task is None:
            return "MISSING"
        if task.status in {"DONE", "FAILED"}:
            return task.status
        items = await runtime.pending_human(task_id)
        for item in items:
            await _resolve_one(runtime, task_id, item)
        await asyncio.sleep(0.4)
    return "TIMEOUT"


async def _resolve_one(runtime: Runtime, task_id: str, item) -> None:
    """按待人工项类型选择动作并执行（§12 动作闭集）。副作用：写库。"""
    targets = list((item.payload or {}).get("target_ids") or [item.target_id])
    if item.kind == "quote":
        logger.info("auto: approve_quote total=¥{}", (item.payload or {}).get("est_total_cny"))
        await runtime.resolve(task_id, "approve_quote", target_ids=targets, note="demo auto approve")
        return
    if item.kind == "asset":
        asset_kind = str((item.payload or {}).get("kind") or "")
        if asset_kind == "keyframe":
            action = "approve_keyframe"
        else:
            # 定妆图与视频都按「人工通过」= pass_slot（§12 动作闭集里没有 approve_video）
            action = "pass_slot"
        await runtime.resolve(task_id, action, target_ids=targets, note="demo auto review")
        return
    if item.kind == "budget":
        logger.info("auto: extend_budget +¥50")
        await runtime.resolve(task_id, "extend_budget", payload={"extra_cny": 50.0}, note="demo auto extend")
        return
    logger.info("auto: pass_slot {} ({})", item.title, item.slot_key or "-")
    await runtime.resolve(task_id, "pass_slot", payload={"slot_key": item.slot_key}, note="demo auto pass")


async def _quote_total(runtime: Runtime, task_id: str) -> float:
    """读取报价单总额（不存在返回 0）。副作用：读磁盘。"""
    from ..assets.quote import read_quote

    quote = read_quote(runtime.ctx.paths(task_id))
    return float(quote.est_total_cny) if quote else 0.0


async def _report(runtime: Runtime, task_id: str) -> int:
    """打印阶段表 / 花费 / 交付包路径，返回退出码。副作用：读库/读盘。"""
    paths = runtime.ctx.paths(task_id)
    stages = await runtime.store.list_stages(task_id)
    rev = await runtime.store.current_revision(task_id)
    task = await runtime.store.get_task(task_id)
    print("\n== 阶段表（当前 revision={}）==".format(rev))
    for row in stages:
        if row.revision != rev:
            continue
        print(f"  {row.stage:<13} {row.slot_key:<13} {row.status:<12} {(row.fail_reason or '')[:60]}")
    spent = await runtime.assets.spent_cny(task_id)
    snap = await runtime.ledger.snapshot(task_id, task.cost_cap_cny if task else None)
    hist = await runtime.assets.attempt_hist(task_id)
    rows = await runtime.assets.list_assets(task_id)
    passed = sum(1 for r in rows if r.status == "PASSED")
    print("\n== 花费 ==")
    print(f"  spent=¥{spent:.3f} reserved=¥{snap.reserved_cny:.3f} cap=¥{snap.cap_cny:.3f} attempt_hist={hist}")
    print(f"  资产行={len(rows)} 通过={passed} 放弃={sum(1 for r in rows if r.status in {'REJECTED', 'ORPHANED'})}")
    print("\n== 产物 ==")
    for name, target in (
        ("交付包", paths.drama_package_md),
        ("镜头表", paths.final_shots_json),
        ("资产报告", paths.asset_report_json),
        ("约束报告", paths.constraint_report_json),
        ("报价单", paths.cost_quote_json),
    ):
        flag = "OK " if target.exists() and target.stat().st_size > 0 else "MISS"
        print(f"  [{flag}] {name}: {target}")
    status = task.status if task else "MISSING"
    print(f"\n== 任务状态: {status} ==")
    if status == "DONE":
        return 0
    return 3


async def _main(argv: list[str] | None = None) -> int:
    """演示主流程。副作用：见模块头。"""
    args = _parse(argv)
    settings = _settings(args)
    logger.info(
        "demo start: topic={} episodes={} dry_run={} auto={} episode_limit={}",
        args.topic,
        args.episodes,
        settings.dry_run,
        args.auto,
        settings.episode_limit,
    )
    runtime = Runtime(settings)
    await runtime.start()
    deadline = time.monotonic() + float(args.timeout)
    try:
        task_id = await runtime.submit(
            {
                "task_id": args.task_id,
                "topic": args.topic,
                "platform": args.platform,
                "episode_count": args.episodes,
                "duration_target": args.duration,
                "cost_cap_cny": args.budget,
                "overrides": {"project": "demo"},
            }
        )
        print(f"task_id = {task_id}")
        status = await _auto_pilot(runtime, task_id, deadline) if args.auto else "MANUAL"
        if status == "TIMEOUT":
            print("\n超时：任务仍在跑或等待人工，请查看 /var/tasks/<task_id>/ 与 pending-human")
        return await _report(runtime, task_id) if status != "MISSING" else 3
    finally:
        await runtime.stop()


def main(argv: list[str] | None = None) -> int:
    """同步入口（uvicorn 无关，直接 python -m）。副作用：跑 asyncio 事件循环。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
