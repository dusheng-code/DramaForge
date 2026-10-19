"""职责：资产泵（§10.3）—— 唯一花钱的一层：原子 claim、预算闸、submit/poll（保留句柄不重抽）、下载落盘、置 REVIEW、失败重试/放弃。
输入：AssetStore、StateStore、预算账本、图像/视频客户端、Settings、产物路径。
输出：被推进到 REVIEW/PASSED/REJECTED/BUDGET_HOLD 的资产行；下载落盘的 png/mp4。

副作用：写库、写磁盘（经 io/artifacts）、HTTPS 出网（provider）、发事件、写日志。
铁律：拿到 provider_task_id 之后**绝不重新 submit**；超时只回 PENDING 保留句柄。
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Callable

from loguru import logger

from ..contracts.assets import ImageRequest, VideoRequest
from ..contracts.artifacts import AssetRow
from ..core.config import Settings
from ..core.events import Events
from ..io import artifacts as io
from .budget import BudgetLedger, estimate_for


def _placeholder_bytes(kind: str) -> bytes:
    """dry-run 占位素材：png 用 1×1 透明图，mp4 用可识别的占位字节（不参与真实播放）。无副作用。"""
    if kind == "video":
        return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )


class AssetPump:
    """单实例资产泵；同一 task 只允许一个 pump 协程在跑。"""

    def __init__(
        self,
        store,
        settings: Settings,
        events: Events,
        ledger: BudgetLedger,
        video_client,
        image_client,
        path_fn: Callable[[str], io.TaskPaths] = io.paths,
        state_store: Any = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.events = events
        self.ledger = ledger
        self.video_client = video_client
        self.image_client = image_client
        self.path_fn = path_fn
        self.state_store = state_store
        self._poll_counts: dict[str, int] = {}
        self._first_seen: dict[str, float] = {}
        self._released = 0
        self.submit_count = 0
        self.poll_count = 0

    # ── 主循环 ─────────────────────────────────────────────
    async def pump(self, task_id: str) -> None:
        """反复处理可 claim 的 PENDING 行与在途行，直到没有工作。副作用：见类注释。

        退出条件：既没有可 claim 的行、也没有在途行；或本轮所有行都因「上游首帧未通过」被退回
        （避免同一批视频行被无限 claim —— 首帧通过后由 video 阶段的 `_wait` 重新拉起本泵）。
        """
        while True:
            self._released = 0
            rows = await self.store.claim(task_id, limit=int(self.settings.asset_concurrency))
            inflight = await self.store.list_by_status(task_id, ["SUBMITTED", "GENERATING"])
            if not rows and not inflight:
                return
            progressed = False
            if rows:
                results = await asyncio.gather(*(self.step(r) for r in rows), return_exceptions=True)
                progressed = any(r is True for r in results)
                for r in results:
                    if isinstance(r, Exception):
                        logger.exception("pump step crashed: {}", r)
            polls = await asyncio.gather(*(self.poll_only(r) for r in inflight), return_exceptions=True)
            progressed = progressed or any(p is True for p in polls)
            if not progressed and rows and self._released >= len(rows) and not inflight:
                logger.info("pump idle: {} rows waiting keyframe, exit task={}", self._released, task_id)
                return
            if not progressed:
                sleep_sec = 0.5 if self.settings.dry_run else float(self.settings.poll_backoff_sec[0])
                await asyncio.sleep(sleep_sec)

    async def step(self, row: AssetRow) -> bool:
        """处理一个刚 claim 到的资产行（SUBMITTED）。返回是否发生了状态推进。副作用：写库/出网/写盘。"""
        if row.kind == "video" and not await self.store.keyframe_passed(row.task_id, row.shot_id):
            await self.store.release(row)
            self._released += 1
            logger.info("video released (keyframe not passed): {}", row.shot_id)
            return False
        est = float(row.est_cost_cny or estimate_for(self.settings, row.kind, row.tier or "fast", self._duration_of(row)))
        cap = await self._task_cap(row.task_id)
        if not await self.ledger.allow(row.task_id, row.episode, est, cap):
            await self.store.hold(row)
            if self.ledger.once(f"{row.task_id}:exceeded"):
                snap = await self.ledger.snapshot(row.task_id, cap)
                self.events.budget_exceeded(row.task_id, snap.spent_cny, snap.cap_cny, row.episode)
            return True
        if row.provider_task_id:
            return await self._poll(row)
        return await self._submit(row)

    async def poll_only(self, row: AssetRow) -> bool:
        """对在途行做一次轮询（不提交）。超时则回 PENDING 且保留句柄。副作用：写库/出网/写盘。"""
        if not row.provider_task_id:
            await self.store.release(row)
            return False
        return await self._poll(row)

    # ── 提交 ───────────────────────────────────────────────
    async def _submit(self, row: AssetRow) -> bool:
        """提交一次真实生成并立即落 provider_task_id。副作用：出网 + 写库。"""
        try:
            if row.kind == "video":
                req = await self.build_video_request(row)
                self.submit_count += 1
                logger.info("submit video: task={} shot={} attempt={} model={}", row.task_id, row.shot_id, row.attempt + 1, req.model)
                pid = await self.video_client.submit(req)
            else:
                req = await self.build_image_request(row)
                self.submit_count += 1
                logger.info("submit image: task={} target={} kind={} attempt={}", row.task_id, row.shot_id, row.kind, row.attempt + 1)
                result = await self.image_client.generate(req)
                return await self._finish_image(row, result)
        except Exception as exc:  # noqa: BLE001 —— 提交失败按重试/放弃处理，不向上抛
            logger.warning("submit failed: {} {} {}", row.task_id, row.shot_id, exc)
            await self.store.retry_or_reject(row, cost_cny=0.0, reason=str(exc)[:200])
            return True
        await self.store.mark_submitted(row, pid, model=self._model_of(row))
        return await self._poll(row, first=True)

    async def build_video_request(self, row: AssetRow) -> VideoRequest:
        """组装图生视频请求（首帧 + motion + 时长 + 档位）。副作用：读磁盘（pack/首帧路径）。"""
        pack = (io.read_json(self.path_fn(row.task_id).packs_json, default={}) or {}).get("packs", [])
        motion = ""
        for item in pack:
            if item.get("shot_id") == row.shot_id:
                motion = str(item.get("motion_prompt") or "")
                break
        duration = self._duration_of(row)
        return VideoRequest(
            shot_id=row.shot_id,
            image_ref=str(self.path_fn(row.task_id).keyframe_png(row.shot_id)),
            motion_prompt=motion,
            duration_sec=duration,
            resolution=(row.resolution or self.settings.video_resolution),  # type: ignore[arg-type]
            aspect=self.settings.video_aspect,
            seed=self.settings.llm_seed,
            tier=(row.tier or "fast"),  # type: ignore[arg-type]
            model=self._model_of(row),
            image_to_video=bool(self.settings.image_to_video),
            generate_audio=bool(self.settings.generate_with_audio),
        )

    async def build_image_request(self, row: AssetRow) -> ImageRequest:
        """组装图像请求（定妆图用主体锚，首帧用 image_prompt）。副作用：读磁盘。"""
        paths = self.path_fn(row.task_id)
        packs = (io.read_json(paths.packs_json, default={}) or {}).get("packs", [])
        prompt = ""
        ref: str | None = None
        if row.kind == "keyframe":
            for item in packs:
                if item.get("shot_id") == row.shot_id:
                    prompt = str(item.get("image_prompt") or "")
                    break
        else:
            chars = (io.read_json(paths.characters_json, default={}) or {}).get("characters", [])
            for ch in chars:
                va = ch.get("visual_anchor") or {}
                if va.get("character_id") == row.shot_id:
                    prompt = str(va.get("anchor_text") or "")
                    ref = va.get("ref_image_path")
                    break
        return ImageRequest(
            kind=row.kind,  # type: ignore[arg-type]
            shot_id=row.shot_id,
            prompt=prompt or "无主体空镜，湿冷雨夜",
            ref_image=ref,
            resolution=(row.resolution or self.settings.image_resolution),  # type: ignore[arg-type]
            aspect=self.settings.image_aspect,
            model=row.model or self.settings.image_model,
            seed=self.settings.llm_seed,
        )

    # ── 轮询 ───────────────────────────────────────────────
    async def _poll(self, row: AssetRow, first: bool = False) -> bool:
        """一次轮询：失败→重试/放弃；完成→下载落盘→REVIEW；未完成→GENERATING 或维持。副作用：出网/写库/写盘。"""
        pid = row.provider_task_id or ""
        self.poll_count += 1
        self._poll_counts[pid] = self._poll_counts.get(pid, 0) + 1
        started = self._first_seen.setdefault(pid, time.monotonic())
        try:
            out = await self.video_client.poll(pid)
        except Exception as exc:  # noqa: BLE001 —— 轮询异常按失败处理（保留句柄由 retry_or_reject 清空）
            logger.warning("poll failed: {} {}", pid, exc)
            await self.store.retry_or_reject(row, 0.0, f"poll error: {exc}"[:200])
            return True
        if out.failed:
            await self.store.retry_or_reject(row, float(out.cost_cny or 0.0), out.error or "provider failed")
            return True
        if out.done:
            try:
                saved = await self._download(row, url=out.url)
            except Exception as exc:  # noqa: BLE001 —— 下载失败也要计费重试
                logger.warning("download failed: {} {}", row.shot_id, exc)
                await self.store.retry_or_reject(row, float(out.cost_cny or 0.0), f"download: {exc}"[:200])
                return True
            est = float(row.est_cost_cny or estimate_for(self.settings, row.kind, row.tier or "fast", self._duration_of(row)))
            from ..providers.pricing import provider_reported_cost

            # dry-run 走 Fake provider，永远零花费（§13 M4「零花费」；真实计费在 M9 验）
            cost = 0.0 if self.settings.dry_run else provider_reported_cost(float(out.cost_cny or 0.0), est)
            await self.store.save_file(row, saved, cost)
            logger.info("asset done: task={} shot={} kind={} cost={}", row.task_id, row.shot_id, row.kind, cost)
            self.events.asset_review_required(row, path=saved)
            return True
        if first and row.status == "SUBMITTED":
            await self.store.mark(row, "GENERATING")
        if time.monotonic() - started > float(self.settings.poll_timeout_sec):
            logger.warning("poll timeout, keep handle and release: {} {}", row.task_id, pid)
            await self.store.mark(row, "PENDING")
            self._first_seen.pop(pid, None)
            return True
        await asyncio.sleep(self._backoff_for(pid))
        return False

    # ── 落盘 ───────────────────────────────────────────────
    async def _finish_image(self, row: AssetRow, result) -> bool:
        """图像为同步返回：直接落盘并置 REVIEW。副作用：写磁盘 + 写库。"""
        if getattr(result, "failed", False):
            await self.store.retry_or_reject(row, float(getattr(result, "cost_cny", 0.0) or 0.0), getattr(result, "error", "image failed"))
            return True
        try:
            saved = await self._download(row, url=getattr(result, "url", None), b64=getattr(result, "b64", None))
        except Exception as exc:  # noqa: BLE001
            await self.store.retry_or_reject(row, float(getattr(result, "cost_cny", 0.0) or 0.0), f"download: {exc}"[:200])
            return True
        est = float(row.est_cost_cny or estimate_for(self.settings, row.kind, row.tier or "fast", 0.0))
        from ..providers.pricing import provider_reported_cost

        cost = 0.0 if self.settings.dry_run else provider_reported_cost(float(getattr(result, "cost_cny", 0.0) or 0.0), est)
        await self.store.save_file(row, saved, cost)
        self.events.asset_review_required(row, path=saved)
        return True

    async def _download(self, row: AssetRow, url: str | None = None, b64: str | None = None) -> str:
        """把结果写进产物目录（图片 png / 视频 mp4）。副作用：HTTPS 下载 + 写磁盘。"""
        paths = self.path_fn(row.task_id).ensure()
        if row.kind == "anchor_image":
            target = paths.anchor_png(row.shot_id)
        elif row.kind == "keyframe":
            target = paths.keyframe_png(row.shot_id)
        else:
            target = paths.video_mp4(row.shot_id)
        if b64:
            data = base64.b64decode(b64)
        elif url and self.settings.dry_run and url.startswith("fake://"):
            # dry-run 专用：Fake provider 给的是不可下载的占位 URL，落一个可识别的占位文件
            data = _placeholder_bytes(row.kind)
        elif url:
            from ..providers.video import download_bytes

            data = await download_bytes(url, self.settings)
        else:
            raise ValueError("no url / b64 in provider result")
        if not data:
            raise ValueError("empty download")
        io.save_bytes(target, data)
        return str(target)

    # ── 小工具 ─────────────────────────────────────────────
    async def _task_cap(self, task_id: str) -> float | None:
        """取任务的 cost_cap_cny（缺 state_store 时返回 None）。副作用：读库。"""
        if self.state_store is None:
            return None
        task = await self.state_store.get_task(task_id)
        return float(task.cost_cap_cny) if task and task.cost_cap_cny else None

    def _duration_of(self, row: AssetRow) -> float:
        """从 packs.json 反查该镜时长（失败回退档位均值）。副作用：读磁盘。"""
        try:
            packs = (io.read_json(self.path_fn(row.task_id).packs_json, default={}) or {}).get("packs", [])
            for item in packs:
                if item.get("shot_id") == row.shot_id:
                    return float(item.get("duration_sec") or 0.0)
        except Exception:  # noqa: BLE001
            pass
        return float(self.settings.per_shot_duration)

    def _model_of(self, row: AssetRow) -> str:
        """按类型与档位选模型名。无副作用。"""
        if row.kind == "video":
            return row.model or self.settings.video_model(row.tier or "fast")
        return row.model or self.settings.image_model

    def _backoff_for(self, pid: str) -> float:
        """按轮询次数取退避秒数（POLL_BACKOFF）。无副作用。"""
        seq = self.settings.poll_backoff_sec
        n = self._poll_counts.get(pid, 1) - 1
        return float(seq[min(max(n, 0), len(seq) - 1)])

    def _next_sleep(self) -> float:
        """无进展时的最小休眠，避免忙轮。无副作用。"""
        seq = self.settings.poll_backoff_sec
        return float(seq[0]) if seq else 5.0

    def reset_counters(self) -> None:
        """清空调用计数（测试用）。副作用：内存清理。"""
        self.submit_count = 0
        self.poll_count = 0
        self._poll_counts.clear()
        self._first_seen.clear()
