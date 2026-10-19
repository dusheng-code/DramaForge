"""职责：进程内运行时装配（唯一 wiring 点）：引擎、两个 store、事件、调度器、资产泵、recovery、handler 注册表与人工决议入口。
输入：Settings（含 dry_run）、API 请求。
输出：`Runtime` 实例：`start()/stop()`、`submit()`、`resolve()`、`ensure_pump()`、`pending_human()`。

副作用：建引擎连接池、写库、写磁盘、发事件、创建 asyncio 任务。禁止在 import 期做 IO。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from ..agents.context import AgentContext
from ..agents.prompt_pack.agent import PromptPackAgent
from ..agents.reference.agent import ConstraintAgent, ReferenceAgent
from ..agents.review.agent import GateAgent
from ..agents.script.agent import ScriptAgent
from ..agents.storyboard.agent import StoryboardAgent
from ..assemble.assembler import Assembler
from ..assets.budget import BudgetLedger
from ..assets.pump import AssetPump
from ..assets.quote import read_quote
from ..assets.review_loop import AssetStages
from ..contracts.artifacts import (
    PendingHumanItem,
    ResolutionRecord,
    StageRow,
    TaskSpec,
)
from ..contracts.enums import SLOT_KEYS, STAGES
from ..contracts.ids import new_task_id
from ..core.bus import EventBus
from ..core.config import Settings
from ..core.events import Events
from ..core.recovery import Recovery
from ..core.revision import RevisionManager
from ..core.scheduler import Scheduler, TaskRunner
from ..core.state_store import StateStore
from ..core.asset_store import AssetStore
from ..core.transitions import REWORK_PRESERVED, downstream_of
from ..io import artifacts as io


def _now() -> str:
    """本地时区 ISO 时间。无副作用。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Runtime:
    """不可变装配后的运行时；`start()` 之前不要调用业务方法。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StateStore.connect(settings.task_state_db)
        self.assets = AssetStore.connect(settings.task_state_db, max_attempt=int(settings.max_attempt_per_shot))
        self.bus = EventBus()
        self.events = Events(self.bus)
        self.ledger = BudgetLedger(self.assets, settings)
        self.video_client, self.image_client = self._clients()
        ctx = AgentContext(
            settings=settings,
            store=self.store,
            assets=self.assets,
            events=self.events,
            path_fn=io.paths,
            video_client=self.video_client,
            image_client=self.image_client,
        )
        if settings.dry_run:
            from ..llm.fake import FakeLLMClient

            ctx.llm_factory = lambda role: FakeLLMClient(settings, role=role)
        else:
            from ..llm.client import LLMClient

            ctx.llm_factory = lambda role: LLMClient.from_role(role, settings)
        self.ctx = ctx

        self.script = ScriptAgent(ctx)
        self.storyboard = StoryboardAgent(ctx)
        self.packs = PromptPackAgent(ctx)
        self.reference = ReferenceAgent(ctx)
        self.constraint = ConstraintAgent(ctx)
        self.gate = GateAgent(ctx)
        self.assembler = Assembler(ctx)
        self.stages = AssetStages(ctx, self.assets, self.ledger, ensure_pump=self.ensure_pump)

        self.handlers = self._handlers()
        self.revision = RevisionManager(self.store, self.assets, settings=self.settings, events=self.events)
        self.scheduler = Scheduler(
            self.store,
            self.events,
            self.handlers,
            self.revision,
            asset_counts=self.assets.counts_by_status,
        )
        self.pump = AssetPump(
            store=self.assets,
            settings=settings,
            events=self.events,
            ledger=self.ledger,
            video_client=self.video_client,
            image_client=self.image_client,
            path_fn=io.paths,
            state_store=self.store,
        )
        self.runner = TaskRunner(self.scheduler, self._PumpAdapter(self), self.store, self.events)
        self.recovery = Recovery(self.store, self.assets, settings, self.events, self.runner, io.paths)
        self._pumps: dict[str, asyncio.Task] = {}
        self._started = False

    # ── 生命周期 ───────────────────────────────────────────
    async def start(self) -> None:
        """启动自愈（RUNNING→PENDING / 产物丢失→STALE / 续跑在途资产）。副作用：写库/建任务。"""
        if self._started:
            return
        self._started = True
        logger.info(
            "runtime start: dry_run={} db={} runtime_dir={}",
            self.settings.dry_run,
            _mask(self.settings.task_state_db),
            self.settings.runtime_path,
        )
        io.paths("_probe").ensure()
        await self.recovery.run()

    async def stop(self) -> None:
        """取消后台任务并释放连接池。副作用：取消 asyncio 任务 / 关闭连接。"""
        await self.runner.shutdown()
        for task in list(self._pumps.values()):
            task.cancel()
        if self._pumps:
            await asyncio.gather(*self._pumps.values(), return_exceptions=True)
        self._pumps.clear()
        await self.store.close()
        await self.assets.close()

    # ── handler 注册 ───────────────────────────────────────
    def _handlers(self) -> dict[str, Any]:
        """stage → handler 的唯一注册处（14 个阶段，gate 按 slot 复用同一 handler）。副作用：无。"""
        return {
            "reference": self.reference.run,
            "constraint": self.constraint.run,
            "outline": self.script.outline,
            "episodes": self.script.episodes,
            "characters": self.script.characters,
            "blocks": self.script.blocks,
            "storyboard": self.storyboard.run,
            "prompt_pack": self.packs.run,
            "gate": self.gate.run,
            "quote": self.stages.quote,
            "anchor_image": self.stages.anchor_image,
            "keyframe": self.stages.keyframe,
            "video": self.stages.video,
            "assemble": self.assembler.run,
        }

    def _clients(self) -> tuple[Any, Any]:
        """按 dry-run 与实际配置取 provider 客户端。副作用：无（惰性建连）。"""
        from ..providers.image import get_image_client
        from ..providers.video import get_video_client

        return (
            get_video_client(self.settings, dry_run=self.settings.dry_run),
            get_image_client(self.settings, dry_run=self.settings.dry_run),
        )

    # ── 资产泵去重 ─────────────────────────────────────────
    async def ensure_pump(self, task_id: str) -> None:
        """确保该 task 有一个资产泵在跑（幂等）。副作用：创建 asyncio 任务。"""
        existing = self._pumps.get(task_id)
        if existing and not existing.done():
            await existing
            return
        task = asyncio.create_task(self._pump_loop(task_id), name=f"pump:{task_id}")
        self._pumps[task_id] = task
        try:
            await task
        finally:
            if self._pumps.get(task_id) is task:
                self._pumps.pop(task_id, None)

    async def _pump_loop(self, task_id: str) -> None:
        """单次资产泵循环（异常不冒泡，避免拖垮调度器）。副作用：写库/出网。"""
        try:
            await self.pump.pump(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("pump crashed: {} {}", task_id, exc)

    class _PumpAdapter:
        """把 TaskRunner 的 pump 依赖接到 Runtime.ensure_pump（保证同一 task 只有一个泵）。"""

        def __init__(self, runtime: "Runtime") -> None:
            self.runtime = runtime

        async def pump(self, task_id: str) -> None:
            """等待该 task 的资产泵跑完。副作用：创建/复用 asyncio 任务。"""
            await self.runtime.ensure_pump(task_id)

    # ── 提交 ───────────────────────────────────────────────
    async def submit(self, req: dict[str, Any]) -> str:
        """建 task + 0 号 revision 的 18 行阶段表并启动调度。副作用：写库/建协程。"""
        tid = str(req.get("task_id") or new_task_id())
        spec = TaskSpec(
            task_id=tid,
            topic=str(req["topic"]),
            platform=str(req.get("platform") or "douyin"),
            episode_count=int(req.get("episode_count") or self.settings.topic_default_episodes),
            episode_duration_target=int(req.get("duration_target") or req.get("episode_duration_target") or 75),
            strict_mode=bool(req.get("strict_mode", False)),
            cost_cap_cny=float(req.get("cost_cap_cny") or self.settings.max_video_cost_cny),
            params=dict(req.get("overrides") or req.get("params") or {}),
            status="PENDING",
        )
        await self.store.create_task(spec)
        await self.store.supply_revision(tid, 0)
        logger.info("task submitted: {} topic={} episodes={}", tid, spec.topic, spec.episode_count)
        await self.runner.submit_task(tid)
        return tid

    async def resume(self, task_id: str) -> None:
        """重新拉起非终态任务（人工决议后调用）。副作用：建协程。"""
        await self.runner.submit_task(task_id)

    # ── 人工决议 ───────────────────────────────────────────
    async def resolve(
        self,
        task_id: str,
        action: str,
        target_ids: list[str] | None = None,
        payload: dict[str, Any] | None = None,
        note: str = "",
        operator: str = "human",
    ) -> dict[str, Any]:
        """执行一次人工决议并留痕，然后重启调度。副作用：写库/写盘/建协程。"""
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task not found: {task_id}")
        revision = await self.store.current_revision(task_id)
        targets = list(target_ids or [])
        payload = dict(payload or {})
        handler = getattr(self, f"_resolve_{action}", None)
        if handler is None:
            raise ValueError(f"unknown action: {action}")
        record = ResolutionRecord(
            task_id=task_id,
            target_type=_target_type(action),
            target_id=_target_key(action, targets, payload, revision),
            action=action,  # type: ignore[arg-type]
            operator=operator,
            comment=note[:512],
        )
        await self.store.add_resolution(record)
        detail = await handler(task, revision, targets, payload, note)
        await self.runner.submit_task(task_id)
        logger.info("resolved: task={} action={} targets={} detail={}", task_id, action, targets[:5], detail)
        return {"task_id": task_id, "action": action, "revision": revision, "detail": detail}

    async def _resolve_approve_quote(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """批准报价：记录 approved revision、插入资产行、把 quote 行放回 PENDING。副作用：写库/写盘。"""
        params = dict(task.params or {})
        params["quote_approved_revision"] = revision
        await self.store.update_task_params(task.task_id, params)
        quote = read_quote(io.paths(task.task_id))
        if quote is not None:
            quote = quote.model_copy(update={"approved": True})
            io.write_json(io.paths(task.task_id).cost_quote_json, quote.model_dump())
        await self._clear_needs_human(task.task_id, stages=("quote",))
        return {"approved_revision": revision, "quote_total": quote.est_total_cny if quote else None}

    async def _resolve_reject_quote(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """拒绝报价：撤销批准标记并保持人工待办（不花钱）。副作用：写库/写盘。"""
        params = dict(task.params or {})
        params["quote_approved_revision"] = -1
        params["quote_rejected_note"] = note[:200]
        await self.store.update_task_params(task.task_id, params)
        logger.info("quote rejected: task={} rev={} note={}", task.task_id, revision, note)
        return {"approved": False, "revision": revision}

    async def _resolve_extend_budget(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """追加预算：cap 或 extra 二选一；释放 BUDGET_HOLD 与预算类 NEEDS_HUMAN。副作用：写库。"""
        base = float(task.cost_cap_cny or self.settings.max_video_cost_cny)
        extra = float(payload.get("extra_cny") or 0)
        cap = float(payload.get("cap_cny") or 0) or (base + extra)
        if cap <= base:
            raise ValueError(f"extend_budget 需要更高的上限（当前 ¥{base:.3f}，请求 ¥{cap:.3f}）")
        await self.store.update_task_cap(task.task_id, cap)
        released = await self.assets.release_holds(task.task_id)
        await self._clear_needs_human(task.task_id, stages=("anchor_image", "keyframe", "video", "quote"))
        self.ledger.reset()
        return {"cap_cny": cap, "previous_cap_cny": base, "released_rows": released}

    async def _resolve_pass_slot(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """人工放行：段位 NEEDS_HUMAN 回 PENDING，并对指定资产行过审。副作用：写库。"""
        slot = payload.get("slot_key") or next((t for t in targets if t in SLOT_KEYS), None)
        cleared = 0
        if slot or payload.get("stages"):
            cleared = await self._clear_needs_human(
                task.task_id,
                stages=tuple(payload.get("stages") or STAGES),
                slot_key=slot,
            )
        asset_targets = [t for t in targets if t not in SLOT_KEYS]
        decided = await self._decide_assets(task, asset_targets, kind=None, approve=True, note=note) if asset_targets else None
        return {"slot_key": slot, "cleared": cleared, "assets": decided}

    async def _resolve_approve_keyframe(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """通过首帧（图筛闸）：视频行随后才允许提交。副作用：写库。"""
        return await self._decide_assets(task, targets, kind="keyframe", approve=True, note=note)

    async def _resolve_veto_keyframe(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """首帧否决（终态）：该镜与视频行一并放弃，进入交付包的未覆盖项。副作用：写库/删文件。"""
        return await self._decide_assets(task, targets, kind="keyframe", approve=False, terminal=True, note=note)

    async def _resolve_retry_shot(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """逐镜返工：可选改提示词（prompt_hint）后重抽；PASSED 行作废并插入同 revision 新行。副作用：写盘/写库。"""
        hint = str(payload.get("prompt_hint") or "").strip()
        patched = self._patch_prompt_hint(task.task_id, targets, hint) if hint and targets else 0
        rows = [r for r in await self.assets.list_assets(task.task_id) if not targets or r.shot_id in set(targets)]
        if not rows:
            raise LookupError(f"no asset rows for targets {targets}")
        done: list[dict] = []
        for row in rows:
            if row.status in {"PENDING", "SUBMITTED", "GENERATING"}:
                done.append({"shot_id": row.shot_id, "kind": row.kind, "status": row.status})
                continue
            if row.status == "PASSED":
                await self.assets.orphan_file(row)
                await self._insert_fresh_row(row)
                done.append({"shot_id": row.shot_id, "kind": row.kind, "status": "re-queued"})
                continue
            if row.status == "REVIEW":
                status = await self.assets.reject_row(row, reason=note or "retry_shot")
                done.append({"shot_id": row.shot_id, "kind": row.kind, "status": status})
                continue
            if row.status in {"REJECTED", "ORPHANED", "BUDGET_HOLD"} and row.attempt < int(self.settings.max_attempt_per_shot):
                await self.store_requeue(row)
                done.append({"shot_id": row.shot_id, "kind": row.kind, "status": "PENDING"})
                continue
            done.append({"shot_id": row.shot_id, "kind": row.kind, "status": row.status})
        return {"patched_prompts": patched, "rows": done}

    async def _resolve_skip_shot(self, task, revision: int, targets: list[str], payload: dict, note: str) -> dict:
        """放弃某镜（人工判断不值得再花）：资产行置 REJECTED，交付包单列未覆盖。副作用：写库。"""
        return await self._decide_assets(task, targets, kind=None, approve=False, terminal=True, note=note or "skip_shot")


    # ── 决议辅助 ───────────────────────────────────────────
    async def _decide_assets(
        self,
        task,
        targets: list[str],
        kind: str | None,
        approve: bool,
        terminal: bool = False,
        note: str = "",
        revision: int | None = None,
    ) -> dict:
        """通过与/否决资产行；terminal=True 表示不重抽（放弃）。副作用：写库/删文件/取消远端任务。"""
        rows = await self.assets.list_assets(task.task_id, kind=kind)
        want_rev = revision if revision is not None else await self.store.current_revision(task.task_id)
        rows = [r for r in rows if r.revision in (want_rev, 0)]
        wanted = {t for t in targets if t}
        hit = [r for r in rows if not wanted or r.shot_id in wanted]
        if not hit:
            raise LookupError(f"no {kind or 'asset'} rows for targets {targets}")
        done: list[dict] = []
        for row in hit:
            if row.status in {"PENDING", "SUBMITTED", "GENERATING"}:
                if not terminal:
                    done.append({"shot_id": row.shot_id, "kind": row.kind, "status": row.status})
                    continue
            if approve:
                if row.status != "REVIEW":
                    # 只有 REVIEW 可过审；其余状态（PASSED/ORPHANED/REJECTED）不改写，避免非法迁移
                    done.append({"shot_id": row.shot_id, "kind": row.kind, "status": row.status})
                    continue
                ok = await self.assets.pass_row(row)
                done.append({"shot_id": row.shot_id, "kind": row.kind, "status": "PASSED" if ok else row.status})
                continue
            if terminal:
                await self._mark_terminal(row, note)
                status = "REJECTED"
            else:
                status = await self.assets.reject_row(row, reason=note or "manual reject")
            if row.file_path and not approve:
                io.remove(row.file_path)
            if row.kind == "keyframe" and (terminal or not approve):
                await self._orphan_shot_videos(task.task_id, row.shot_id)
            done.append({"shot_id": row.shot_id, "kind": row.kind, "status": status})
        return {"kind": kind or "any", "rows": done}

    async def _mark_terminal(self, row, note: str) -> None:
        """把资产行强制置 REJECTED（跳过重试），用于 skip_shot / veto_keyframe。副作用：写库。"""
        try:
            await self.assets.mark(row, "REJECTED", reject_reason=(note or "manual terminal")[:512])
        except Exception as exc:  # noqa: BLE001 —— 非法迁移时退回 reject_row，保证状态终态化
            logger.warning("terminal mark failed ({}), fallback to reject_row: {}", row.shot_id, exc)
            await self.assets.reject_row(row, reason=note or "manual terminal")

    async def _orphan_shot_videos(self, task_id: str, shot_id: str) -> int:
        """首帧被否决/打回时，该镜视频行一并作废（有条件取消远端任务）。副作用：写库 + 出网。"""
        n = 0
        for v in await self.assets.list_assets(task_id, kind="video"):
            if v.shot_id != shot_id or v.status in {"ORPHANED", "PASSED"}:
                continue
            try:
                await self.assets.mark(v, "ORPHANED")
                n += 1
            except Exception:  # noqa: BLE001
                continue
            if v.provider_task_id:
                try:
                    await self.video_client.cancel(v.provider_task_id)
                except Exception:  # noqa: BLE001 —— 取消失败不影响本地状态
                    logger.warning("cancel failed: {}", v.provider_task_id)
        return n

    async def _insert_fresh_row(self, row) -> None:
        """为同 revision 重新排队一行（PASSED 行作废后重抽）。副作用：写库。"""
        fresh = row.model_copy(
            update={
                "status": "PENDING",
                "attempt": 0,
                "provider_task_id": None,
                "file_path": None,
                "cost_cny": 0.0,
                "reject_reason": None,
            }
        )
        await self.assets.insert_rows([fresh])

    async def _requeue(self, row) -> None:
        """把 REJECTED/ORPHANED/BUDGET_HOLD 行放回 PENDING 重新排队。副作用：写库。"""
        try:
            await self.assets.mark(row, "PENDING", provider_task_id=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("requeue failed for {}: {}", row.shot_id, exc)

    def _patch_prompt_hint(self, task_id: str, targets: list[str], hint: str) -> int:
        """把人工补充提示追加进 packs.json 的 motion_prompt（改词重抽）。副作用：写盘。"""
        paths = io.paths(task_id)
        raw = io.read_json(paths.packs_json, default={}) or {}
        packs = list(raw.get("packs", []))
        wanted = {t for t in targets if t}
        n = 0
        for pack in packs:
            if wanted and pack.get("shot_id") not in wanted:
                continue
            base = str(pack.get("motion_prompt") or "")
            pack["motion_prompt"] = f"{base}；{hint}"[:400]
            n += 1
        if n:
            raw["packs"] = packs
            io.write_json(paths.packs_json, raw)
            logger.info("prompt hint applied to {} shots: {}", n, hint[:40])
        return n

    async def _clear_needs_human(self, task_id: str, stages: tuple[str, ...], slot_key: str | None = None) -> int:
        """把 NEEDS_HUMAN 的阶段行放回 PENDING（人工已处理）。副作用：写库。"""
        n = 0
        for row in await self.store.list_stages(task_id):
            if row.stage not in stages or row.status != "NEEDS_HUMAN":
                continue
            if slot_key and row.slot_key != slot_key:
                continue
            if await self.store.cas(row, "NEEDS_HUMAN", "PENDING", fail_reason="resolved by human"):
                n += 1
        return n

    async def accepted_slots(self, task_id: str, revision: int) -> set[str]:
        """已被人工作出 `pass_slot` 决议的段位（gate 复跑时据此放行 needs_human 项）。副作用：读库。"""
        out: set[str] = set()
        for rec in await self.store.list_resolutions(task_id):
            if rec.get("action") != "pass_slot":
                continue
            target = str(rec.get("target_id") or "")
            if not target.endswith(f":{revision}"):
                continue
            out.add(target.rsplit(":", 1)[0])
        return out

    # ── 查询 ───────────────────────────────────────────────
    async def pending_human(self, task_id: str) -> list[PendingHumanItem]:
        """汇总待人工项（阶段行 + 资产行 + 报价）。副作用：读库/读盘。"""
        items: list[PendingHumanItem] = []
        stages = await self.store.list_stages(task_id)
        current = await self.store.current_revision(task_id)
        quote = read_quote(io.paths(task_id))
        for row in stages:
            if row.status != "NEEDS_HUMAN":
                continue
            kind = "compliance" if row.stage == "gate" else ("quote" if row.stage == "quote" else "gate")
            payload: dict[str, Any] = {"target_ids": [f"{row.slot_key}:{row.revision}"]} if row.stage == "gate" else {}
            if row.stage == "quote" and quote is not None:
                payload = {
                    "target_ids": [f"quote:{task_id}:{row.revision}"],
                    "est_total_cny": quote.est_total_cny,
                    "cap_cny": quote.cap_cny,
                    "items": [i.model_dump() for i in quote.items],
                }
            items.append(
                PendingHumanItem(
                    kind=kind,  # type: ignore[arg-type]
                    target_id=f"stage:{row.stage}:{row.slot_key}:{row.revision}",
                    task_id=task_id,
                    title=f"{row.stage}/{row.slot_key} 等待人工",
                    detail=row.fail_reason or "",
                    actions=_actions_for(row.stage),
                    slot_key=row.slot_key if row.slot_key != "*" else None,
                    payload=payload,
                    created_at=_now(),
                )
            )
        for row in await self.assets.list_by_status(task_id, ["REVIEW", "BUDGET_HOLD"]):
            kind = "budget" if row.status == "BUDGET_HOLD" else "asset"
            if row.status == "BUDGET_HOLD":
                actions = ["extend_budget"]
            elif row.kind == "video":
                actions = ["pass_slot", "retry_shot", "skip_shot"]
            else:
                actions = ["approve_keyframe", "veto_keyframe", "retry_shot"]
            items.append(
                PendingHumanItem(
                    kind=kind,  # type: ignore[arg-type]
                    target_id=row.shot_id,
                    task_id=task_id,
                    title=f"{row.kind} {row.shot_id} 待审核",
                    detail=f"attempt={row.attempt} cost={row.cost_cny}",
                    actions=actions,
                    payload={"target_ids": [row.shot_id], "revision": row.revision, "kind": row.kind},
                    created_at=_now(),
                )
            )
        _ = current
        return items

    async def stages_of(self, task_id: str) -> list[StageRow]:
        """当前 revision 的阶段行（按阶段顺序）。副作用：读库。"""
        rows = await self.store.list_stages(task_id)
        rev = await self.store.current_revision(task_id)
        current = [r for r in rows if r.revision == rev]
        order = {name: i for i, name in enumerate(STAGES)}
        return sorted(current, key=lambda r: (order.get(r.stage, 99), SLOT_KEYS.index(r.slot_key) if r.slot_key in SLOT_KEYS else -1))


def _target_type(action: str) -> str:
    """决议动作 → target_type（quote / asset / gate / budget）。无副作用。"""
    if action in {"approve_quote", "reject_quote"}:
        return "quote"
    if action in {"approve_keyframe", "veto_keyframe", "retry_shot", "skip_shot"}:
        return "asset"
    if action == "extend_budget":
        return "budget"
    return "gate"


def _target_key(action: str, targets: list[str], payload: dict, revision: int) -> str:
    """留痕主键：pass_slot 记 `{slot}:{revision}`（gate 复跑据此放行），其余记目标列表。无副作用。"""
    if action == "pass_slot":
        slot = payload.get("slot_key") or next((t for t in targets if t in SLOT_KEYS), "*")
        return f"{slot}:{revision}"
    return ",".join(targets)[:255] or "*"


def _actions_for(stage: str) -> list[str]:
    """某阶段等待人工时前端可执行的动作（§12 动作闭集）。无副作用。"""
    if stage == "quote":
        return ["approve_quote", "reject_quote", "extend_budget"]
    if stage == "gate":
        return ["pass_slot", "retry_shot", "skip_shot"]
    if stage in {"anchor_image", "keyframe"}:
        return ["approve_keyframe", "veto_keyframe", "retry_shot"]
    if stage == "video":
        return ["pass_slot", "retry_shot", "skip_shot"]
    return ["pass_slot"]


def _mask(dsn: str) -> str:
    """日志用 DSN 脱敏（去掉密码）。无副作用。"""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    head, tail = dsn.split("://", 1)
    cred, host = tail.split("@", 1)
    if ":" in cred:
        cred = cred.split(":", 1)[0] + ":***"
    return f"{head}://{cred}@{host}"
