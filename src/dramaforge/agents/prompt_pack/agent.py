"""职责：prompt_pack 阶段 handler：用 §9.1 模板把镜头表转成可投喂的提示词包（LLM 只贡献 motion 动作句）。
输入：AgentContext、StageRow；storyboard/shots.json + script/characters.json。
输出：prompt_pack/packs.json。

副作用：调用 LLM（仅 motion 句）、写磁盘、发进度事件。
"""

from __future__ import annotations

from loguru import logger

from ...contracts.artifacts import PromptPack, Shot
from ...contracts.ids import spec_version
from ...io import artifacts as io
from ..context import AgentContext
from ..script.agent import _input_block, _system
from .templates import build_pack, missing_markers


class PromptPackAgent:
    """提示词包生成器（模板权威）。"""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def run(self, row) -> str:
        """生成 packs.json。副作用：LLM + 写磁盘。"""
        paths = self.ctx.paths(row.task_id)
        shots = self.ctx.load_shots(row.task_id)
        if not shots:
            raise RuntimeError("shots.json 为空，无法组装提示词")
        characters = {c.character_id: c for c in self.ctx.load_characters(row.task_id)}
        style = self.ctx.settings.style_suffix
        resolution = self.ctx.settings.video_resolution

        llm_motions: dict[str, str] = {}
        try:
            client = self.ctx.llm("prompt_pack")
            payload = {
                "shots": [s.model_dump() for s in shots],
                "characters": [
                    {"character_id": c.character_id, "anchor_text": c.anchor_text} for c in characters.values()
                ],
                "style_suffix": style,
            }
            drafts: list[PromptPack] = await client.agenerate_object(
                _system("prompt_pack"),
                "只产出 motion_prompt 的动作句，其余字段会被模板覆盖。" + _input_block(payload),
                list[PromptPack],
                key="prompt_pack",
            )
            llm_motions = {d.shot_id: d.motion_prompt for d in drafts if d.motion_prompt}
        except Exception as exc:  # noqa: BLE001 —— motion 句缺失可退回模板默认，不阻断链路
            logger.warning("prompt_pack llm motion unavailable, fallback to template: {}", exc)

        packs: list[PromptPack] = []
        prev: str | None = None
        for shot in shots:
            pack = build_pack(
                shot=shot,
                chars=characters,
                style_suffix=style,
                resolution=resolution,
                llm_motion=llm_motions.get(shot.shot_id),
                prev_shot_id=prev,
            )
            missing = missing_markers(pack.image_prompt)
            if missing:
                logger.error("pack missing markers {}: {}", missing, shot.shot_id)
            packs.append(pack)
            prev = shot.shot_id

        io.write_json(
            paths.packs_json,
            {
                "task_id": row.task_id,
                "packs": [p.model_dump() for p in packs],
                "spec_version": spec_version([p.model_dump() for p in packs]),
            },
        )
        self.ctx.events.progress_message(row.task_id, "prompt_pack", f"提示词包 {len(packs)} 条")
        return str(paths.packs_json)


def packs_by_shot(packs: list[PromptPack]) -> dict[str, PromptPack]:
    """shot_id → pack 索引。无副作用。"""
    return {p.shot_id: p for p in packs}


def shots_by_id(shots: list[Shot]) -> dict[str, Shot]:
    """shot_id → Shot 索引。无副作用。"""
    return {s.shot_id: s for s in shots}
