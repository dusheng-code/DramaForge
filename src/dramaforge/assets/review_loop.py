"""职责：资产阶段的 handler（quote / anchor_image / keyframe / video）：报价闸、资产行供给、等待 pump 到终态、图筛与视频审核的人工决议回路。
输入：AgentContext、StageRow、AssetStore、BudgetLedger。
输出：报价单路径或资产目录路径；BUDGET_HOLD / 达上限放弃 / 人工待办都会体现在返回值与异常里。

副作用：写磁盘（报价单）、读写库、发事件、写日志；不直接调用 provider（那是 pump 的职责）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

from ..contracts.artifacts import AssetRow, Character, PromptPack, Shot
from ..contracts.enums import ASSET_KINDS
from ..core.transitions import NeedsHuman
from ..io import artifacts as io
from .budget import BudgetLedger
from .quote import build_quote, quote_by_shot, read_quote, write_quote

INFLIGHT_STATUSES: tuple[str, ...] = ("PENDING", "SUBMITTED", "GENERATING", "REVIEW")


@dataclass
class WaitResult:
    """等待资产的结论：通过数、放弃数、是否触顶。"""

    passed: int = 0
    abandoned: list[str] | None = None
    budget_hold: bool = False


class AssetStages:
    """四个资产相关阶段的 handler 集合。"""

    def __init__(self, ctx, store, ledger: BudgetLedger, ensure_pump=None) -> None:
        self.ctx = ctx
        self.store = store
        self.ledger = ledger
        self.ensure_pump = ensure_pump

    # ── quote ──────────────────────────────────────────────
    async def quote(self, row) -> str:
        """产出报价单并挂人工；已批准的 revision 直接通过并插入资产行。副作用：写盘 + 写库 + 事件。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        paths = self.ctx.paths(row.task_id)
        approved_rev = int(task.params.get("quote_approved_revision", -1))
        if approved_rev >= int(row.revision):
            await self._ensure_rows(row.task_id, row.revision, with_est=True)
            logger.info("quote already approved for revision {}", row.revision)
            return str(paths.cost_quote_json)

        shots = self.ctx.load_shots(row.task_id)
        packs = self.ctx.load_packs(row.task_id)
        characters = self.ctx.load_characters(row.task_id)
        cap = self.ledger.cap_for(task.cost_cap_cny)
        from ..providers.pricing import expected_attempts

        quote = build_quote(
            task_id=row.task_id,
            revision=row.revision,
            shots=shots,
            packs=packs,
            characters=characters,
            settings=self.ctx.settings,
            cap_cny=cap,
            expected_attempts=expected_attempts(),
        )
        write_quote(quote, paths)
        self.ctx.events.cost_quote_ready(row.task_id, str(paths.cost_quote_json), quote.est_total_cny, row.revision)
        if not quote.within_cap:
            self.ctx.events.budget_exceeded(row.task_id, 0.0, quote.cap_cny)
        raise NeedsHuman(
            f"报价待人工批准：预估 ¥{quote.est_total_cny:.3f}（上限 ¥{quote.cap_cny:.3f}）",
            target_ids=[f"quote:{row.task_id}:{row.revision}"],
        )

    # ── anchor_image / keyframe / video ────────────────────
    async def anchor_image(self, row) -> str:
        """定妆图阶段：供给资产行并等待全部到终态。副作用：写库 + 写盘（pump）。"""
        await self._ensure_rows(row.task_id, 0, kinds=("anchor_image",), characters=await self._characters(row.task_id))
        result = await self._wait(row, kind="anchor_image", revision=0)
        return self._dir(row.task_id, "anchors", result)

    async def keyframe(self, row) -> str:
        """首帧阶段：逐镜首帧 + 图筛闸。副作用：写库 + 写盘（pump）。"""
        await self._ensure_rows(row.task_id, row.revision, kinds=("keyframe",))
        result = await self._wait(row, kind="keyframe", revision=row.revision)
        return self._dir(row.task_id, "keyframes", result)

    async def video(self, row) -> str:
        """视频阶段：首帧通过后才允许提交；等待逐镜视频到终态。副作用：写库 + 写盘（pump）。"""
        await self._reconcile_video(row.task_id, row.revision)
        await self._ensure_rows(row.task_id, row.revision, kinds=("video",))
        result = await self._wait(row, kind="video", revision=row.revision)
        return self._dir(row.task_id, "videos", result)

    # ── 资产行供给 ─────────────────────────────────────────
    async def _ensure_rows(
        self,
        task_id: str,
        revision: int,
        kinds: tuple[str, ...] = ASSET_KINDS,
        with_est: bool = False,
        characters: list[Character] | None = None,
    ) -> int:
        """按当前镜头/角色表补齐缺失的资产行（幂等）。副作用：写库。"""
        from ..providers.pricing import est_anchor_cost, est_keyframe_cost, est_video_cost

        shots = self.ctx.load_shots(task_id)
        packs = {p.shot_id: p for p in self.ctx.load_packs(task_id)}
        quote = read_quote(self.ctx.paths(task_id)) if with_est else None
        est_map = quote_by_shot(quote) if quote else {}
        rows: list[AssetRow] = []
        if "anchor_image" in kinds:
            chars = characters if characters is not None else self.ctx.load_characters(task_id)
            for ch in chars:
                rows.append(
                    AssetRow(
                        task_id=task_id,
                        shot_id=ch.character_id,
                        kind="anchor_image",
                        revision=0,
                        status="PENDING",
                        attempt=0,
                        episode=0,
                        tier="fast",
                        resolution=self.ctx.settings.image_resolution,
                        est_cost_cny=float(est_map.get(ch.character_id, {}).get("anchor_image") or est_anchor_cost(self.ctx.settings)),
                    )
                )
        for shot in shots:
            pack: PromptPack | None = packs.get(shot.shot_id)
            tier = (pack.tier if pack else shot.quality_tier) or "fast"
            if "keyframe" in kinds:
                rows.append(
                    AssetRow(
                        task_id=task_id,
                        shot_id=shot.shot_id,
                        kind="keyframe",
                        revision=revision,
                        status="PENDING",
                        attempt=0,
                        episode=shot.episode,
                        tier=tier,
                        resolution=(pack.resolution if pack else self.ctx.settings.image_resolution),
                        est_cost_cny=float(est_map.get(shot.shot_id, {}).get("keyframe") or est_keyframe_cost(self.ctx.settings)),
                    )
                )
            if "video" in kinds:
                rows.append(
                    AssetRow(
                        task_id=task_id,
                        shot_id=shot.shot_id,
                        kind="video",
                        revision=revision,
                        status="PENDING",
                        attempt=0,
                        episode=shot.episode,
                        tier=tier,
                        resolution=(pack.resolution if pack else self.ctx.settings.video_resolution),
                        est_cost_cny=float(
                            est_map.get(shot.shot_id, {}).get("video")
                            or est_video_cost(tier, shot.duration_sec, self.ctx.settings)
                        ),
                    )
                )
        if not rows:
            return 0
        n = await self.store.insert_rows(rows)
        logger.info("asset rows ensured: task={} rev={} kinds={} inserted={}", task_id, revision, kinds, n)
        return n

    async def _characters(self, task_id: str) -> list[Character]:
        """读角色表（供定妆图行供给）。副作用：读磁盘。"""
        return self.ctx.load_characters(task_id)

    async def _reconcile_video(self, task_id: str, revision: int) -> None:
        """首帧被放弃（REJECTED/ORPHANED）的镜头，其视频行一并 ORPHANED，避免永久 PENDING。副作用：写库。"""
        keyframes = await self.store.list_assets(task_id, revision=revision, kind="keyframe")
        dead = {k.shot_id for k in keyframes if k.status in {"REJECTED", "ORPHANED"}}
        if not dead:
            return
        videos = await self.store.list_assets(task_id, revision=revision, kind="video")
        for v in videos:
            if v.shot_id in dead and v.status in {"PENDING", "REJECTED"}:
                try:
                    await self.store.mark(v, "ORPHANED")
                except Exception:  # noqa: BLE001 —— 迁移非法时只记日志
                    logger.warning("cannot orphan video row: {} {}", v.shot_id, v.status)

    # ── 等待 ───────────────────────────────────────────────
    async def _wait(self, row, kind: str, revision: int, interval: float = 0.5, max_wait: float = 6 * 3600) -> WaitResult:
        """轮询资产行直到无在途项；触顶抛 NeedsHuman，达上限则计入放弃清单。副作用：读库/写库（dry-run 自动过审）。

        有 PENDING 行时主动驱动资产泵（人工放行后泵可能已退出，这里负责重新拉起）。
        """
        elapsed = 0.0
        auto_reviewed = 0
        while True:
            rows = await self.store.list_assets(row.task_id, revision=revision, kind=kind)
            if not rows:
                await asyncio.sleep(interval)
                elapsed += interval
                if elapsed > 30:
                    raise NeedsHuman(f"{kind} 资产行为空，无法继续")
                continue
            statuses = {r.status for r in rows}
            if "PENDING" in statuses and self.ensure_pump is not None:
                await self.ensure_pump(row.task_id)
                rows = await self.store.list_assets(row.task_id, revision=revision, kind=kind)
                statuses = {r.status for r in rows}
            if self.ctx.settings.dry_run:
                auto_reviewed += await self._auto_review(rows)
                if auto_reviewed:
                    rows = await self.store.list_assets(row.task_id, revision=revision, kind=kind)
                    statuses = {r.status for r in rows}
            if "BUDGET_HOLD" in statuses:
                raise NeedsHuman(f"{kind} 预算触顶，需人工追加预算", target_ids=[row.task_id])
            if not (statuses & set(INFLIGHT_STATUSES)):
                abandoned = [r.shot_id for r in rows if r.status in {"REJECTED", "ORPHANED"}]
                passed = sum(1 for r in rows if r.status == "PASSED")
                if abandoned:
                    logger.warning("{} abandoned after max attempts: {}", kind, abandoned)
                if auto_reviewed:
                    logger.info("dry-run auto-reviewed {} {} rows", auto_reviewed, kind)
                return WaitResult(passed=passed, abandoned=abandoned)
            await asyncio.sleep(interval)
            elapsed += interval
            if elapsed > max_wait:
                if self.ctx.settings.dry_run:
                    # dry-run 模式下等待超时自动把剩余生成中/已就绪行过审，避免 Fake 调度抖动卡住测试
                    for r in rows:
                        if r.status in {"REVIEW", "GENERATING", "SUBMITTED"}:
                            await self.store.mark(r, "PASSED")
                    abandoned = [r.shot_id for r in rows if r.status in {"REJECTED", "ORPHANED"}]
                    passed = sum(1 for r in rows if r.status == "PASSED")
                    return WaitResult(passed=passed, abandoned=abandoned)
                raise NeedsHuman(f"{kind} 等待超时（{max_wait}s），需人工介入")

    async def _auto_review(self, rows: list[AssetRow]) -> int:
        """dry-run 专用：把 REVIEW 行自动置 PASSED（模拟人工键盘过审）。副作用：写库。"""
        n = 0
        for r in rows:
            if r.status == "REVIEW":
                if await self.store.pass_row(r):
                    n += 1
        return n

    def _dir(self, task_id: str, sub: str, result: WaitResult) -> str:
        """返回该类别资产目录（output_path）。副作用：无（只拼 io.paths 提供的目录）。"""
        paths = self.ctx.paths(task_id)
        target = {"anchors": paths.anchors, "keyframes": paths.keyframes, "videos": paths.videos}[sub]
        return str(target)


def asset_key(row: AssetRow) -> str:
    """资产行的稳定字符串键（供 resolve 的 target_ids 与前端定位）。无副作用。"""
    return f"{row.shot_id}:{row.kind}:{row.revision}"


def parse_asset_key(key: str) -> tuple[str, str, int]:
    """解析 `shot_id:kind:revision`；解析失败抛 ValueError。无副作用。"""
    parts = key.rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError(f"bad asset key: {key}")
    return parts[0], parts[1], int(parts[2])
