"""职责：script 链路四个阶段（outline / episodes / characters / blocks）的产出与落盘：大纲、逐集剧本、角色视觉锚、剧本 block 切分。
输入：AgentContext（LLM、store、路径）、阶段行 StageRow。
输出：阶段产物路径（outline.md / episodes/epNN.md / characters.json / blocks.json）。

副作用：调用 LLM（结构化/文本）、写磁盘、发进度事件。blocks 阶段为**纯确定性解析**，不再调用 LLM。
"""

from __future__ import annotations

import json
import re

from loguru import logger

from ...contracts.artifacts import (
    Block,
    Character,
    CharacterDraft,
    Claim,
    ConstraintSpec,
    Outline,
    Scene,
    SegmentChapter,
    VisualAnchor,
)
from ...contracts.enums import SLOT_KEYS, GLOBAL_SLOT
from ...contracts.ids import character_id as make_character_id
from ...contracts.ids import spec_version
from ...io import artifacts as io
from ...io import md as mdlib
from ..context import AgentContext
from .format import parse_blocks

_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _input_block(payload: dict) -> str:
    """把结构化输入渲染成 fenced json 块，供真实 LLM 与 dry-run 生成器共用。无副作用。"""
    return "【结构化输入】\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def _constraint_digest(constraints: list[ConstraintSpec]) -> list[dict]:
    """压缩约束为提示用摘要（避免把长结构塞进 prompt）。无副作用。"""
    return [
        {
            "constraint_id": c.constraint_id,
            "slot_key": c.slot_key,
            "validator_key": c.validator_key,
            "value": c.value,
            "unit": c.unit,
            "hard": c.hard,
        }
        for c in constraints
    ]


class ScriptAgent:
    """剧本链路的四个 handler。所有 handler 返回 output_path（多文件用 os.pathsep 连接）。"""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    # ── outline ────────────────────────────────────────────
    async def outline(self, row) -> str:
        """生成大纲并落 outline.md（含机器可读 json 块）。副作用：LLM 调用 + 写磁盘。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        constraints = self.ctx.load_constraints(row.task_id)
        episode_count = self._episode_count(task)
        paths = self.ctx.paths(row.task_id)
        client = self.ctx.llm("script")
        payload = {
            "project": task.task_id,
            "topic": task.topic,
            "platform": task.platform,
            "episode_count": episode_count,
            "episode_duration_target": task.episode_duration_target,
            "constraints": _constraint_digest(constraints),
            "rework_violations": self._violations(row.task_id),
        }
        outline: Outline = await client.agenerate_object(
            _system("script_outline"), "请按约束产出整季大纲。" + _input_block(payload), Outline, key="script.outline"
        )
        outline = outline.model_copy(update={"project": task.params.get("project") or task.task_id})
        body = mdlib.join(
            [
                mdlib.h(1, f"《{task.topic}》大纲"),
                f"**logline**：{outline.logline}",
                mdlib.table(
                    ("集", "标题", "本集目标", "钩子"),
                    [(e.episode, e.title, e.goal, e.hook) for e in outline.episodes],
                ),
                mdlib.section("机器可读", mdlib.code_block(json.dumps(outline.model_dump(), ensure_ascii=False, indent=2), "json")),
            ]
        )
        io.write_text(paths.outline_md, body)
        self.ctx.events.progress_message(row.task_id, "outline", f"大纲完成，共 {len(outline.episodes)} 集")
        return str(paths.outline_md)

    # ── episodes ───────────────────────────────────────────
    async def episodes(self, row) -> str:
        """逐集生成剧本 md。副作用：LLM 调用 + 写磁盘（每集一个文件）。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        outline = self.load_outline(row.task_id)
        characters = self.ctx.load_characters(row.task_id)
        constraints = self.ctx.load_constraints(row.task_id)
        paths = self.ctx.paths(row.task_id)
        client = self.ctx.llm("script")
        violations = self._violations(row.task_id)
        limit = int(self.ctx.settings.episode_limit or 0)
        episodes = outline.episodes
        if limit > 0:
            episodes = episodes[:limit]
        written: list[str] = []
        for ep in episodes:
            payload = {
                "project": task.params.get("project") or task.task_id,
                "episode": ep.episode,
                "title": ep.title,
                "goal": ep.goal,
                "beats": ep.beats,
                "hook": ep.hook,
                "topic": task.topic,
                "platform": task.platform,
                "characters": [c.model_dump() for c in characters],
                "constraints": _constraint_digest(constraints),
                "rework_violations": violations if row.revision > 0 else [],
            }
            text = await client.agenerate_text(_system("script_episode"), "请产出单集剧本。" + _input_block(payload))
            if not text.strip():
                raise RuntimeError(f"empty episode text for ep{ep.episode}")
            # 提前用 blocks 阶段的同一解析器验格式，坏输出在本阶段就失败，不浪费后续阶段。
            if not parse_blocks(text, ep.episode)[0]:
                raise RuntimeError(
                    f"ep{ep.episode} 剧本格式不符合约定（未识别到任何 动作:/台词(id): 行），丢弃本轮输出"
                )
            target = paths.episode_md(ep.episode)
            io.write_text(target, text)
            written.append(str(target))
            self.ctx.events.progress_message(row.task_id, "episodes", f"第 {ep.episode} 集完成")
        return io.join_output(*written)

    # ── characters ─────────────────────────────────────────
    async def characters(self, row) -> str:
        """生成角色视觉锚与语感指纹。副作用：LLM 调用 + 写磁盘。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        outline = self.load_outline(row.task_id)
        constraints = self.ctx.load_constraints(row.task_id)
        paths = self.ctx.paths(row.task_id)
        client = self.ctx.llm("script")
        payload = {
            "project": task.params.get("project") or task.task_id,
            "topic": task.topic,
            "episodes": [e.model_dump() for e in outline.episodes],
            "constraints": _constraint_digest(constraints),
        }
        drafts: list[CharacterDraft] = await client.agenerate_object(
            _system("script_characters"),
            "请产出角色表（每人一条视觉锚，20-120 字，必须可复现）。" + _input_block(payload),
            list[CharacterDraft],
            key="script.characters",
        )
        characters = self._dedup(drafts)
        if not characters:
            raise RuntimeError("no characters generated")
        io.write_json(
            paths.characters_json,
            {
                "task_id": row.task_id,
                "characters": [c.model_dump() for c in characters],
                "spec_version": spec_version([c.model_dump() for c in characters]),
            },
        )
        self.ctx.events.progress_message(row.task_id, "characters", f"角色 {len(characters)} 位")
        return str(paths.characters_json)

    # ── blocks ─────────────────────────────────────────────
    async def blocks(self, row) -> str:
        """把逐集剧本 md 解析成 block 列表（纯确定性，不调 LLM）。副作用：读/写磁盘。"""
        paths = self.ctx.paths(row.task_id)
        outline = self.load_outline(row.task_id)
        limit = int(self.ctx.settings.episode_limit or 0)
        episodes = outline.episodes[:limit] if limit > 0 else outline.episodes
        all_blocks: list[Block] = []
        all_scenes: list[Scene] = []
        for ep in episodes:
            text = io.read_text(paths.episode_md(ep.episode))
            if not text:
                raise FileNotFoundError(f"missing episode md: {paths.episode_md(ep.episode)}")
            blocks, scenes = parse_blocks(text, ep.episode)
            if not blocks:
                raise RuntimeError(f"no block parsed from ep{ep.episode}（剧本格式不符合约定）")
            all_blocks.extend(blocks)
            all_scenes.extend(scenes)
        io.write_json(
            paths.blocks_json,
            {
                "task_id": row.task_id,
                "blocks": [b.model_dump() for b in all_blocks],
                "scenes": [s.model_dump() for s in all_scenes],
                "spec_version": spec_version([b.model_dump() for b in all_blocks]),
            },
        )
        logger.info("blocks parsed: task={} blocks={} scenes={}", row.task_id, len(all_blocks), len(all_scenes))
        return str(paths.blocks_json)

    # ── helpers ────────────────────────────────────────────
    def load_outline(self, task_id: str) -> Outline:
        """从 outline.md 的 json 块还原 Outline。副作用：读磁盘。"""
        text = io.read_text(self.ctx.paths(task_id).outline_md)
        if not text:
            raise FileNotFoundError("outline.md missing")
        matches = _JSON_FENCE.findall(text)
        for raw in reversed(matches):
            try:
                return Outline.model_validate(json.loads(raw))
            except Exception:  # noqa: BLE001 —— 继续尝试下一块
                continue
        raise RuntimeError("outline.md 缺少机器可读 json 块")

    def _episode_count(self, task) -> int:
        """集数：任务参数优先，EPISODE_LIMIT 只影响后续阶段。无副作用。"""
        return int(task.episode_count or self.ctx.settings.topic_default_episodes)

    def _violations(self, task_id: str) -> list[dict]:
        """读上一轮违规清单（返工时增量重写用）。副作用：读磁盘。"""
        raw = io.read_json(self.ctx.paths(task_id).violations_json, default={}) or {}
        return list(raw.get("violations", []) or [])[:40]

    def _dedup(self, drafts: list[CharacterDraft]) -> list[Character]:
        """按显示名生成稳定 character_id 并去重。无副作用。"""
        out: list[Character] = []
        seen: set[str] = set()
        for d in drafts:
            cid = make_character_id(d.display_name)
            if cid in seen:
                continue
            seen.add(cid)
            out.append(
                Character(
                    visual_anchor=VisualAnchor(
                        character_id=cid,
                        display_name=d.display_name,
                        anchor_text=d.anchor_text,
                        wardrobe_change=d.wardrobe_change,
                    ),
                    role=d.role,
                    catchphrase=d.catchphrase,
                    vocal_habit=d.vocal_habit,
                    first_ep=d.first_ep,
                )
            )
        return out


def _system(name: str) -> str:
    """读取 role 的 system prompt；缺失时用最小兜底（不阻断 dry-run）。无副作用。"""
    try:
        from ...llm.roles import load_system_prompt

        return load_system_prompt(name)
    except Exception:  # noqa: BLE001 —— prompt 文件缺失时降级为最小指令
        logger.warning("system prompt missing: {}（使用兜底指令）", name)
        return "你是短剧编剧。只输出要求的 JSON 或 markdown，不要解释。"


def chapters_from_notes(task_id: str, notes) -> list[SegmentChapter]:
    """把 reference role 的 SegmentNote 列表转成五份 SegmentChapter。无副作用。"""
    by_slot: dict[str, list] = {k: [] for k in SLOT_KEYS}
    for note in notes:
        by_slot.setdefault(note.slot_key, []).append(note)
    out: list[SegmentChapter] = []
    for slot in SLOT_KEYS:
        notes_for_slot = by_slot.get(slot, [])
        claims = [
            Claim(statement=n.text, confidence=0.7, basis_ids=list(n.basis_ids))
            for n in notes_for_slot
            if n.text
        ]
        summary = "；".join(c.statement for c in claims)[:390] or (
            f"{slot} 段位暂无可用参照证据，已记入 gaps。"
        )
        if len(summary) < 40:
            summary = (summary + "（该段位证据不足，需补充参照卡后复核）")[:400]
        gaps = [] if claims else [f"{slot}: 缺少参照证据"]
        chapter = SegmentChapter(
            task_id=task_id,
            slot_key=slot,  # type: ignore[arg-type]
            role="drama",
            summary=summary,
            claims=claims,
            gaps=gaps,
        )
        chapter = chapter.model_copy(update={"spec_version": spec_version(chapter.model_dump(exclude={"spec_version"}))})
        out.append(chapter)
    return out


def render_chapters_md(chapters: list[SegmentChapter]) -> str:
    """渲染 script/segment_chapters.md。无副作用。"""
    parts = [mdlib.h(1, "段位章节（五份齐全）")]
    for ch in chapters:
        rows = [(c.statement, f"{c.confidence:.2f}", ", ".join(c.basis_ids) or "—") for c in ch.claims]
        parts.append(
            mdlib.section(
                f"{ch.slot_key} 段位",
                mdlib.join(
                    [
                        ch.summary,
                        mdlib.table(("断言", "置信度", "证据"), rows) if rows else "（无断言）",
                        "gaps: " + ("; ".join(ch.gaps) if ch.gaps else "无"),
                    ]
                ),
            )
        )
    return mdlib.join(parts)


__all__ = ["ScriptAgent", "chapters_from_notes", "render_chapters_md", "GLOBAL_SLOT"]
