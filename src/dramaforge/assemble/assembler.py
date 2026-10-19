"""职责：assemble 阶段 handler：汇总全部产物，产出 final/drama_package.md、final/shots.json、final/asset_report.json（§15 / §10.5）。
输入：AgentContext、StageRow；磁盘上的剧本/分镜/提示词/资产/报告。
输出：三个交付文件路径（os.pathsep 连接）。

副作用：读磁盘、写磁盘、发 TASK_FINISHED 前的进度事件。禁止调用 LLM 或 provider（合成即排版）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from ..contracts.artifacts import AssetReport, AssetShotReport
from ..contracts.ids import spec_version
from ..io import artifacts as io
from ..io import md as mdlib
from .templates import sections


class Assembler:
    """交付包装配器。"""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    async def run(self, row) -> str:
        """装配三个交付文件。副作用：读写磁盘。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        paths = self.ctx.paths(row.task_id)
        shots = self.ctx.load_shots(row.task_id)
        packs = self.ctx.load_packs(row.task_id)
        characters = self.ctx.load_characters(row.task_id)
        constraints = self.ctx.load_constraints(row.task_id)
        raw = {
            "blocks": io.read_json(paths.blocks_json, default={}) or {},
            "chapters": io.read_json(paths.constraints_json, default={}) or {},
            "report": io.read_json(paths.constraint_report_json, default={}) or {},
            "quote": io.read_json(paths.cost_quote_json, default={}) or {},
            "outline": self._outline_episodes(row.task_id),
        }
        assets = await self.ctx.assets.list_assets(row.task_id)
        asset_rows = [a.model_dump() for a in assets]
        report = await self._asset_report(row.task_id, assets)
        gaps = self._gaps(raw["report"], constraints, report, row.task_id)

        generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        task_info = {
            "task_id": task.task_id,
            "topic": task.topic,
            "platform": task.platform,
            "episode_count": task.episode_count,
            "episode_duration_target": task.episode_duration_target,
            "strict_mode": task.strict_mode,
            "cost_cap_cny": task.cost_cap_cny,
            "generated_at": generated_at,
        }
        package = mdlib.join(
            [
                sections.cover(task_info, {"shots": len(shots)}, max(0, row.revision)),
                sections.chapters_section(raw["chapters"].get("chapters", [])),
                sections.constraints_section(constraints, raw["report"]),
                sections.episodes_section(raw["outline"], raw["blocks"].get("blocks", []), shots),
                sections.prompt_pack_section(packs),
                sections.characters_section(characters),
                sections.assets_section(report.model_dump(), asset_rows),
                sections.budget_section(raw["quote"], report.total_spent_cny, float(task.cost_cap_cny or self.ctx.settings.max_video_cost_cny)),
                sections.gaps_section(gaps),
            ]
        )
        io.write_text(paths.drama_package_md, package)
        io.write_json(
            paths.final_shots_json,
            {
                "task_id": row.task_id,
                "generated_at": generated_at,
                "shots": [s.model_dump() for s in shots],
                "packs": [p.model_dump() for p in packs],
                "spec_version": spec_version([s.model_dump() for s in shots]),
            },
        )
        io.write_json(paths.asset_report_json, report.model_dump())
        logger.info(
            "assembled: task={} shots={} files=3 spent={} gaps={}",
            row.task_id,
            len(shots),
            report.total_spent_cny,
            len(gaps),
        )
        self.ctx.events.progress_message(row.task_id, "assemble", f"交付包完成，未覆盖项 {len(gaps)} 条")
        return io.join_output(paths.drama_package_md, paths.final_shots_json, paths.asset_report_json)

    # ── 内部 ───────────────────────────────────────────────
    def _outline_episodes(self, task_id: str) -> list[dict]:
        """从 outline.md 的 json 块取分集大纲（缺失时返回空）。副作用：读盘。"""
        import json
        import re

        text = io.read_text(self.ctx.paths(task_id).outline_md)
        for raw in reversed(re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.DOTALL)):
            try:
                return list(json.loads(raw).get("episodes", []))
            except Exception:  # noqa: BLE001
                continue
        return []

    async def _asset_report(self, task_id: str, assets: list) -> AssetReport:
        """按 §10.5 生成 asset_report（花费/逐镜/放弃/attempt 直方图/通过率）。副作用：读库。"""
        per_shot = [
            AssetShotReport(
                shot_id=a.shot_id,
                kind=a.kind,
                revision=a.revision,
                status=a.status,
                attempt=a.attempt,
                cost_cny=a.cost_cny,
                tier=a.tier,
                file_path=a.file_path,
            )
            for a in assets
        ]
        rejected = [r for r in per_shot if r.status == "REJECTED"]
        abandoned = sorted({r.shot_id for r in per_shot if r.kind == "video" and r.status in {"REJECTED", "ORPHANED"}})
        videos = [r for r in per_shot if r.kind == "video" and r.status not in {"ORPHANED"}]
        passed = [r for r in videos if r.status == "PASSED"]
        hist = await self.ctx.assets.attempt_hist(task_id)
        spent = await self.ctx.assets.spent_cny(task_id)
        return AssetReport(
            task_id=task_id,
            total_spent_cny=spent,
            per_shot=per_shot,
            rejected=rejected,
            abandoned=abandoned,
            attempt_hist=hist,
            pass_rate=round(len(passed) / len(videos), 4) if videos else 0.0,
            generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        )

    def _gaps(self, report: dict, constraints: list, asset_report: AssetReport, task_id: str) -> list[str]:
        """汇总未覆盖项：约束未判、段位 gaps、放弃镜、预算偏差。无副作用。"""
        gaps: list[str] = []
        judged = {v.get("constraint_id") for v in (report.get("verdicts") or [])}
        for c in constraints:
            if c.constraint_id not in judged:
                gaps.append(f"约束未判定：{c.constraint_id}（{c.validator_key}）")
        for v in report.get("verdicts") or []:
            if v.get("verdict") == "NEEDS_HUMAN":
                gaps.append(f"待人工确认：{v.get('constraint_id')}（{v.get('validator_key')}）")
        for shot in asset_report.abandoned:
            gaps.append(f"镜头放弃（达 MAX_ATTEMPT）：{shot}")
        if not report.get("passed", False):
            gaps.append("约束报告整体未通过，交付包仅供人工复核")
        return gaps
