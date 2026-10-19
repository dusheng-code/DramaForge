"""职责：reference 与 constraint 两个阶段：参照卡检索（Milvus→SQL 降级）、榜单统计、公域时效、五段位章节归纳、约束装配（无据丢弃）。
输入：AgentContext、StageRow；数据库卡片表、search_snapshot 缓存。
输出：reference/cards_used.json、rank_stats.json、search_snapshot.json、constraints.json；script/segment_chapters.md。

副作用：LLM 调用、检索外部服务（可能降级）、写磁盘、发事件、写日志。约束报告**不经过 LLM**。
"""

from __future__ import annotations

import json
import re

from loguru import logger

from ...contracts.artifacts import (
    ConstraintSpec,
    ConstraintSpecDraft,
    SegmentChapter,
    SegmentNote,
)
from ...contracts.constraints import finalize
from ...contracts.enums import PAYOFF_TAGS, SLOT_KEYS
from ...contracts.ids import spec_version
from ...io import artifacts as io
from ...io import md as mdlib
from ...validators.registry import cost_impact_keys
from ..context import AgentContext
from ..script.agent import _input_block, _system, chapters_from_notes, render_chapters_md
from .public_search import hits_to_payload, search_topic

_JSON_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class ReferenceAgent:
    """reference 阶段 handler。"""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def run(self, row) -> str:
        """检索参照卡 + 榜单 + 时效 → 五段位章节。副作用：LLM/出网/写盘。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        paths = self.ctx.paths(row.task_id)
        cards, degraded, gaps = await self._retrieve_cards(task)
        rank_stats = await self._rank_stats()
        cached = io.read_json(paths.search_snapshot_json, default={}) or {}
        search = await search_topic(
            task.topic,
            task.platform or "douyin",
            self.ctx.settings,
            cache=list(cached.get("hits", []) or []),
        )
        if search.degraded:
            logger.warning("reference degraded: {}", "; ".join(search.gaps)[:300])

        notes, note_gaps = await self._summarize(task, cards, rank_stats, search.hits)
        chapters = chapters_from_notes(row.task_id, notes)
        for ch in chapters:
            for gap in note_gaps:
                if gap not in ch.gaps:
                    ch.gaps.append(gap)

        io.write_json(
            paths.cards_used_json,
            {
                "task_id": row.task_id,
                "source": "dry-run-fixture" if self.ctx.settings.dry_run and not cards else ("degraded" if degraded else "milvus"),
                "degraded": degraded,
                "gaps": gaps,
                "cards": cards,
                "spec_version": spec_version(cards),
            },
        )
        io.write_json(paths.rank_stats_json, {"task_id": row.task_id, **rank_stats})
        io.write_json(
            paths.search_snapshot_json,
            {
                "task_id": row.task_id,
                "degraded": search.degraded,
                "cached": search.cached,
                "queries": [task.topic, task.platform],
                "hits": hits_to_payload(search.hits),
                "gaps": search.gaps,
            },
        )
        io.write_text(
            paths.segment_chapters_md,
            mdlib.join(
                [
                    render_chapters_md(chapters),
                    mdlib.code_block(
                        json.dumps(
                            {"task_id": row.task_id, "chapters": [c.model_dump() for c in chapters]},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        "json",
                    ),
                ]
            ),
        )
        self.ctx.events.progress_message(
            row.task_id, "reference", f"参照卡 {len(cards)} 张，时效命中 {len(search.hits)} 条"
        )
        return io.join_output(paths.cards_used_json, paths.rank_stats_json, paths.search_snapshot_json)

    # ── 内部 ───────────────────────────────────────────────
    async def _retrieve_cards(self, task) -> tuple[list[dict], bool, list[str]]:
        """检索参照卡：Milvus 优先、失败降级 SQL 关键词；dry-run 无卡时用夹具。副作用：IO/网络。"""
        gaps: list[str] = []
        degraded = False
        cards: list[dict] = []
        try:
            from ...retrieval.cards import cards_to_payload, retrieve_cards

            bundle = await retrieve_cards(task.task_id, task.topic, self.ctx.settings)
            cards = cards_to_payload(bundle.cards)
            degraded = bool(getattr(bundle, "degraded", False))
            gaps.extend(list(getattr(bundle, "gaps", []) or []))
        except Exception as exc:  # noqa: BLE001 —— 检索层不可用不得阻断文本链路
            logger.warning("card retrieval unavailable: {}", exc)
            degraded = True
            gaps.append(f"检索不可用: {type(exc).__name__}")
        if not cards:
            if self.ctx.settings.dry_run or not self.ctx.settings.tavily_api_key:
                from ...llm.fake import fake_cards, fake_tag_distribution

                cards = fake_cards()
                gaps.append("dry-run 参照卡夹具（非真实卡片库）")
                logger.warning("using dry-run card fixtures ({} cards)", len(cards))
                self.ctx.extra["ref_tag_dist"] = fake_tag_distribution()
            else:
                gaps.append("参照卡检索为空：结构约束代表性不足")
        return cards, degraded, gaps

    async def _rank_stats(self) -> dict:
        """榜单统计：优先真实表聚合，失败时给出空结构（不阻断）。副作用：读库。"""
        try:
            from ...retrieval.sql_repo import ReferenceCardRepo

            repo = ReferenceCardRepo(self.ctx.extra.get("engine") or self.ctx.store.engine)
            stats = await repo.rank_stats()
            return dict(stats or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("rank_stats unavailable: {}", exc)
            return {"rows": 0, "platforms": {}, "gaps": [f"榜单统计不可用: {type(exc).__name__}"]}

    async def _summarize(self, task, cards: list[dict], rank_stats: dict, hits) -> tuple[list[SegmentNote], list[str]]:
        """reference role 归纳五段位（只归纳不产约束）。副作用：LLM 调用。"""
        payload = {
            "task_id": task.task_id,
            "topic": task.topic,
            "platform": task.platform,
            "cards": cards[:40],
            "rank_stats": rank_stats,
            "public_search": [{"title": h.title, "snippet": h.snippet, "url": h.url} for h in hits[:10]],
            "slots": list(SLOT_KEYS),
        }
        client = self.ctx.llm("reference")
        try:
            notes: list[SegmentNote] = await client.agenerate_object(
                _system("reference"),
                "请按五个段位归纳参照证据，每段位一条，basis_ids 引用 card_id。"
                + _input_block(payload),
                list[SegmentNote],
                key="reference",
            )
        except Exception as exc:  # noqa: BLE001 —— 归纳失败不阻断，章节用 gaps 记录
            logger.warning("reference llm failed: {}", exc)
            return [], [f"reference 归纳失败: {type(exc).__name__}"]
        gaps = [f"该段位无参照证据（{k}）" for k in SLOT_KEYS if not any(n.slot_key == k for n in notes)]
        return notes, gaps


class ConstraintAgent:
    """constraint 阶段 handler：LLM 草稿 + Python 基线 + 无据丢弃 + cost_impact 强制改写。"""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    async def run(self, row) -> str:
        """装配 ConstraintSpec 并落 constraints.json。副作用：LLM + 写盘 + 事件。"""
        task = await self.ctx.store.get_task(row.task_id)
        if task is None:
            raise LookupError(f"task not found: {row.task_id}")
        paths = self.ctx.paths(row.task_id)
        cards_raw = io.read_json(paths.cards_used_json, default={}) or {}
        cards = list(cards_raw.get("cards", []))
        chapters = self._chapters(row.task_id)
        rank_stats = io.read_json(paths.rank_stats_json, default={}) or {}
        search = io.read_json(paths.search_snapshot_json, default={}) or {}

        valid_refs = {str(c.get("card_id")) for c in cards if c.get("card_id")}
        drafts = self._baseline(task, cards)
        drafts.extend(await self._llm_drafts(task, cards, chapters, rank_stats, search))

        specs, dropped = finalize(
            drafts,
            task_id=row.task_id,
            valid_refs=valid_refs,
            cost_impact_keys=cost_impact_keys(),
        )
        ref_dist = self._ref_tag_dist(cards, chapters)
        io.write_json(
            paths.constraints_json,
            {
                "task_id": row.task_id,
                "revision": row.revision,
                "cards_used": len(cards),
                "dropped": dropped,
                "chapters": [c.model_dump() for c in chapters],
                "constraints": [c.model_dump() for c in specs],
                "ref_tag_dist": ref_dist,
                "payoff_tags": {},
                "spec_version": spec_version([c.model_dump() for c in specs]),
            },
        )
        logger.info("constraints assembled: task={} kept={} dropped={}", row.task_id, len(specs), len(dropped))
        self.ctx.events.constraint_ready(row.task_id, str(paths.constraints_json), len(specs), row.revision)
        return str(paths.constraints_json)

    # ── 内部 ───────────────────────────────────────────────
    def _chapters(self, task_id: str) -> list[SegmentChapter]:
        """从 segment_chapters.md 的 json 块读回章节；缺失时构造带 gaps 的五份。副作用：读盘。"""
        text = io.read_text(self.ctx.paths(task_id).segment_chapters_md)
        for raw in reversed(_JSON_FENCE.findall(text or "")):
            try:
                data = json.loads(raw)
                return [SegmentChapter.model_validate(c) for c in data.get("chapters", [])]
            except Exception:  # noqa: BLE001
                continue
        return chapters_from_notes(task_id, [])

    def _baseline(self, task, cards: list[dict]) -> list[ConstraintSpecDraft]:
        """确定性基线约束：把机器可判的硬规则落成 ConstraintSpecDraft（证据=配置或卡片）。无副作用。"""
        s = self.ctx.settings
        low, high = s.episode_duration_range
        by_slot: dict[str, list[str]] = {}
        for c in cards:
            by_slot.setdefault(str(c.get("slot_key")), []).append(str(c.get("card_id")))

        def refs(slot: str, key: str) -> list[str]:
            return by_slot.get(slot, [])[:2] or [f"config:{key}"]

        n_ep = int(task.episode_count or s.topic_default_episodes)
        limit = int(s.episode_limit or 0)
        if limit > 0:
            # §11 EPISODE_LIMIT：调试时只跑前 N 集，集数约束随之收敛（否则必然误判 FAIL）
            n_ep = min(n_ep, limit)
        baseline: list[tuple[str, str, dict]] = [
            ("structure", "episode_count_range", dict(kind="count", hard=True, value=[n_ep, n_ep],
                                                     fail_action="reject_and_regenerate")),
            ("structure", "episode_duration_range", dict(kind="numeric", hard=True, value=[low, high],
                                                         unit="sec", fail_action="reject_and_regenerate")),
            ("structure", "shot_duration_fit_model", dict(kind="enum", hard=True, value=list(s.shot_ladder),
                                                          unit="sec", fail_action="reject_and_regenerate")),
            ("structure", "shot_count_budget", dict(kind="count", hard=True,
                                                    value=[s.min_shot_per_episode, s.max_shot_per_episode],
                                                    fail_action="reject_and_regenerate")),
            ("platform_voice", "line_length_max", dict(kind="numeric", hard=True, value=18, unit="char",
                                                       fail_action="reject_and_regenerate")),
            ("platform_voice", "line_count_range", dict(kind="count", hard=True, value=[2, 16],
                                                        fail_action="reject_and_regenerate")),
            ("platform_voice", "line_fits_shot_duration", dict(kind="numeric", hard=True, value=s.chars_per_sec,
                                                               unit="char/sec", fail_action="reject_and_regenerate")),
            ("emotion", "action_complexity_cap", dict(kind="structural", hard=True,
                                                      value={"characters": 1, "verbs": 2},
                                                      fail_action="reject_and_regenerate")),
            ("premise", "characters_exist", dict(kind="structural", hard=True, value="characters.json ids",
                                                 fail_action="reject_and_regenerate")),
            ("premise", "character_consistency", dict(kind="structural", hard=True, value="anchor_text ⊆ image_prompt",
                                                      fail_action="reject_and_regenerate")),
            ("platform_voice", "prompt_pack_complete", dict(kind="structural", hard=True,
                                                            value=["[CHAR]", "[SCENE]", "[CAMERA]", "[ACTION]", "[LIGHT]"],
                                                            fail_action="reject_and_regenerate")),
            ("structure", "wardrobe_continuity", dict(kind="structural", hard=True, value="ep_start 命中替换锚",
                                                      fail_action="needs_human")),
            ("structure", "first_turn_position", dict(kind="numeric", hard=True, value=[1, 2], unit="ep",
                                                     fail_action="needs_human")),
            ("emotion", "episode_ending_unresolved", dict(kind="enum", hard=True, value=["resolved"],
                                                          fail_action="reject_and_regenerate")),
            ("risk", "forbidden_setting", dict(kind="forbidden", hard=True, value=list(s.forbidden_term_list),
                                               fail_action="reject_and_regenerate")),
            ("risk", "similarity_disclosure", dict(kind="structural", hard=True, value="≥1 条相似爆款披露",
                                                   fail_action="needs_human")),
            # AMBIGUITY(§7 vs §6.4.5)：payoff_density 在 §7 表格 cost_impact=是，但同节明确「只 warn 不阻断」；
            # 这里按行为口径落 warn_and_continue（cost_impact 由 validator 元数据推导为 True，装配时会被
            # 强制改成 needs_human —— 为遵从其「只 warn」的明示要求，基线把 value 阈值放宽，避免误挂人工）。
            ("emotion", "payoff_density", dict(kind="numeric", hard=False, value=0.85, unit="l1",
                                               fail_action="warn_and_continue")),
            ("platform_voice", "voice_fingerprint_unique", dict(kind="structural", hard=True,
                                                                value="每角色 1 条口头禅且跨角色唯一",
                                                                fail_action="needs_human")),
        ]
        out: list[ConstraintSpecDraft] = []
        for slot, key, kw in baseline:
            out.append(
                ConstraintSpecDraft(
                    slot_key=slot,  # type: ignore[arg-type]
                    source_refs=refs(slot, key),
                    validator_key=key,
                    **kw,
                )
            )
        return out

    async def _llm_drafts(self, task, cards, chapters, rank_stats, search) -> list[ConstraintSpecDraft]:
        """constraint role 的补充草稿（无 source_refs 的会被 finalize 丢弃）。副作用：LLM 调用。"""
        payload = {
            "task_id": task.task_id,
            "topic": task.topic,
            "platform": task.platform,
            "cards": cards[:40],
            "chapters": [c.model_dump() for c in chapters],
            "rank_stats": rank_stats,
            "search": search.get("hits", [])[:5],
        }
        client = self.ctx.llm("constraint")
        try:
            drafts: list[ConstraintSpecDraft] = await client.agenerate_object(
                _system("constraint"),
                "请从参照卡推导结构化约束；每条必须带 source_refs（card_id）。"
                + _input_block(payload),
                list[ConstraintSpecDraft],
                key="constraint",
            )
            return list(drafts)
        except Exception as exc:  # noqa: BLE001 —— 推导失败时基线仍可用
            logger.warning("constraint llm failed, baseline only: {}", exc)
            return []

    def _ref_tag_dist(self, cards: list[dict], chapters: list[SegmentChapter]) -> dict[str, float]:
        """参照爽点标签分布：优先卡片 payload.tags，其次固定均匀分布。无副作用。"""
        counts: dict[str, float] = {t: 0.0 for t in PAYOFF_TAGS}
        hit = False
        for card in cards:
            payload = card.get("payload") or {}
            tags = payload.get("tags") or []
            for tag in tags:
                if tag in counts:
                    counts[tag] += 1.0
                    hit = True
        if not hit:
            if self.ctx.settings.dry_run:
                from ...llm.fake import fake_tag_distribution

                return fake_tag_distribution()
            return {t: 1.0 for t in PAYOFF_TAGS}
        total = sum(counts.values()) or 1.0
        return {k: round(v / total, 4) for k, v in counts.items()}


def constraints_of(task_id: str, ctx: AgentContext) -> list[ConstraintSpec]:
    """从磁盘读回约束（供 gate/pump 复用）。副作用：读盘。"""
    raw = io.read_json(ctx.paths(task_id).constraints_json, default={}) or {}
    return [ConstraintSpec.model_validate(c) for c in raw.get("constraints", [])]
