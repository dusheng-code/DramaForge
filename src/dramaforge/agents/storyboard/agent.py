"""职责：storyboard 阶段的 handler：按集把 blocks 交给 storyboard role 出镜头，Python 侧补 shot_id、场景表与 §9.2 赋档。
输入：AgentContext、StageRow（revision）。
输出：storyboard/shots.json 与 storyboard/scenes.json 的路径。

副作用：调用 LLM、写磁盘、发进度事件、写 tier 降级日志。
"""

from __future__ import annotations

import json

from loguru import logger

from ...contracts.artifacts import Block, Outline, Scene, Shot
from ...contracts.ids import shot_id as make_shot_id
from ...contracts.ids import spec_version
from ...io import artifacts as io
from ..context import AgentContext
from ..script.agent import _constraint_digest, _input_block, _system
from .post import assign_tiers


class StoryboardAgent:
    """镜头表生成器。"""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def run(self, row) -> str:
        """生成 shots.json + scenes.json。副作用：LLM + 写磁盘。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        paths = self.ctx.paths(row.task_id)
        raw_blocks = io.read_json(paths.blocks_json, default={}) or {}
        blocks = [Block.model_validate(b) for b in raw_blocks.get("blocks", [])]
        scenes = [Scene.model_validate(s) for s in raw_blocks.get("scenes", [])]
        characters = self.ctx.load_characters(row.task_id)
        constraints = self.ctx.load_constraints(row.task_id)
        project = str(task.params.get("project") or row.task_id[:12])
        outline = self._outline(row.task_id)
        limit = int(self.ctx.settings.episode_limit or 0)

        client = self.ctx.llm("storyboard")
        all_shots: list[Shot] = []
        episodes = sorted({b.episode for b in blocks})
        if limit > 0:
            episodes = episodes[:limit]
        for ep in episodes:
            ep_blocks = [b for b in blocks if b.episode == ep]
            payload = {
                "project": project,
                "episode": ep,
                "topic": task.topic,
                "blocks": [b.model_dump() for b in ep_blocks],
                "characters": [c.model_dump() for c in characters],
                "duration_ladder": list(self.ctx.settings.shot_ladder),
                "episode_duration_range": list(self.ctx.settings.episode_duration_range),
                "shot_count_range": [
                    self.ctx.settings.min_shot_per_episode,
                    self.ctx.settings.max_shot_per_episode,
                ],
                "chars_per_sec": self.ctx.settings.chars_per_sec,
                "constraints": _constraint_digest(constraints),
            }
            shots: list[Shot] = await client.agenerate_object(
                _system("storyboard"),
                "请把本集 block 拆成镜头表（时长必须命中档位）。" + _input_block(payload),
                list[Shot],
                key="storyboard",
            )
            all_shots.extend(self._normalize(shots, project, ep, ep_blocks))
            self.ctx.events.progress_message(row.task_id, "storyboard", f"第 {ep} 集分镜完成")

        all_shots.sort(key=lambda s: (s.episode, s.scene, int(s.shot_id.rsplit(":", 1)[-1])))
        all_shots, decisions = assign_tiers(all_shots, float(self.ctx.settings.hero_max_ratio))
        io.write_json(
            paths.shots_json,
            {
                "task_id": row.task_id,
                "project": project,
                "shots": [s.model_dump() for s in all_shots],
                "tier_decisions": [d.__dict__ for d in decisions],
                "spec_version": spec_version([s.model_dump() for s in all_shots]),
            },
        )
        io.write_json(
            paths.scenes_json,
            {"task_id": row.task_id, "scenes": [s.model_dump() for s in scenes], "spec_version": spec_version([s.model_dump() for s in scenes])},
        )
        logger.info("storyboard done: task={} shots={}", row.task_id, len(all_shots))
        return io.join_output(paths.shots_json, paths.scenes_json)

    # ── helpers ────────────────────────────────────────────
    def _normalize(self, shots: list[Shot], project: str, episode: int, blocks: list[Block]) -> list[Shot]:
        """补齐/重写 shot_id 与 source_block_ids，保证 id 规范且证据链非空。无副作用。"""
        out: list[Shot] = []
        counters: dict[int, int] = {}
        fallback_blocks = [b.block_id for b in blocks] or ["b0001"]
        for i, shot in enumerate(shots):
            scene = int(shot.scene or 1)
            counters[scene] = counters.get(scene, 0) + 1
            sid = make_shot_id(project, episode, scene, counters[scene])
            src = [b for b in (shot.source_block_ids or []) if b]
            if not src:
                src = [fallback_blocks[i % len(fallback_blocks)]]
            out.append(shot.model_copy(update={"shot_id": sid, "episode": episode, "source_block_ids": src}))
        return out

    def _outline(self, task_id: str) -> Outline | None:
        """读大纲（仅用于 logline 提示，可缺失）。副作用：读磁盘。"""
        from ..script.agent import ScriptAgent

        try:
            return ScriptAgent(self.ctx).load_outline(task_id)
        except Exception:  # noqa: BLE001
            return None


def summary_of(shots: list[Shot]) -> dict:
    """镜头表的统计摘要（供 assemble 与前端）。无副作用。"""
    by_ep: dict[int, int] = {}
    for s in shots:
        by_ep[s.episode] = by_ep.get(s.episode, 0) + 1
    return {
        "total": len(shots),
        "per_episode": {str(k): v for k, v in sorted(by_ep.items())},
        "duration_total": round(sum(s.duration_sec for s in shots), 2),
        "json": json.dumps({"shots": len(shots)}, ensure_ascii=False),
    }
