"""职责：gate 阶段的 handler：按 slot 跑 18 个确定性校验器、合并 constraint_report.json、按 fail_action 决定返工/挂人工、并让人工提示语落地。
输入：AgentContext、StageRow（stage=gate，slot_key=某段位）。
输出：约束报告与违规清单路径；抛 GateViolation / NeedsHuman 由调度器分流。

副作用：LLM 调用（仅 tagger 打标与 review 提示语）、写磁盘、发事件、写日志。判定本身零 LLM。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from loguru import logger

from ...contracts.artifacts import ConstraintReport, ConstraintSpec, PayoffTagItem, Violation
from ...contracts.enums import PAYOFF_TAGS
from ...contracts.ids import spec_version
from ...io import artifacts as io
from ...validators.registry import GateContext, build_report, needs_human_of, run_all
from ..context import AgentContext
from ..script.agent import _input_block, _system


class GateAgent:
    """单段位闸门执行器。五个 slot 并发执行时用进程内锁串行合并报告。"""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self._lock = asyncio.Lock()

    async def run(self, row) -> str:
        """执行该 slot 的确定性校验并落报告。副作用：LLM(tagger/review) + 写盘 + 异常分流。"""
        async with self._lock:
            return await self._run_locked(row)

    async def _run_locked(self, row) -> str:
        """在锁内执行单段位闸门（避免五份报告互相覆盖）。副作用：见 run()。"""
        from ...core.transitions import GateViolation, NeedsHuman

        ctx = await self.build_context(row)
        slot_constraints = [c for c in ctx.constraints if c.slot_key == row.slot_key]
        ctx = replace(ctx, constraints=slot_constraints)

        violations = run_all(ctx)
        # 人工已在本人 revision 对该段位作出 pass_slot 决议：needs_human 项视为已被接受（否则会无限复挂）
        accepted = await self._accepted(row)
        if accepted:
            dropped = [v for v in violations if v.needs_human]
            violations = [v for v in violations if not v.needs_human]
            if dropped:
                logger.info(
                    "gate {} rev={}: 人工已放行，忽略 {} 条 needs_human 项",
                    row.slot_key,
                    row.revision,
                    len(dropped),
                )
        report = build_report(ctx, violations)
        hints = await self._review_hints(row, report, violations)
        self._merge_report(row, report, violations, slot_constraints, hints)
        io.write_json(
            self.ctx.paths(row.task_id).violations_json,
            {
                "task_id": row.task_id,
                "revision": row.revision,
                "slot_key": row.slot_key,
                "violations": [v.model_dump() for v in violations],
            },
        )
        logger.info(
            "gate {} rev={}: violations={} hard={} needs_human={}",
            row.slot_key,
            row.revision,
            len(violations),
            report.hard_failures,
            report.needs_human,
        )
        self.ctx.events.progress_message(
            row.task_id,
            "gate",
            f"{row.slot_key} 段位：{len(slot_constraints)} 条约束，违规 {len(violations)} 条",
            slot_key=row.slot_key,
        )

        by_id: dict[str, ConstraintSpec] = {c.constraint_id: c for c in slot_constraints}
        regen = [
            v
            for v in violations
            if not v.needs_human
            and v.severity == "hard"
            and by_id.get(v.constraint_id)
            and by_id[v.constraint_id].fail_action == "reject_and_regenerate"
        ]
        if regen:
            # 硬违规优先返工：返工会重建下游，人工项在下一轮重新判定（§6.4.1）
            raise GateViolation(regen)
        human = needs_human_of(violations)
        if human:
            raise NeedsHuman(
                f"{row.slot_key} 段位需人工确认：{human[0].constraint_id}",
                target_ids=sorted({v.constraint_id for v in human}),
            )
        return str(self.ctx.paths(row.task_id).constraint_report_json)

    # ── 上下文装配 ─────────────────────────────────────────
    async def _accepted(self, row) -> bool:
        """该段位在当前 revision 是否已被人工作出 pass_slot 决议。副作用：读库。"""
        try:
            records = await self.ctx.store.list_resolutions(row.task_id)
        except Exception as exc:  # noqa: BLE001 —— 读不到决议就按「未放行」处理（更保守）
            logger.warning("list_resolutions failed: {}", exc)
            return False
        want = f"{row.slot_key}:{row.revision}"
        any_want = f"*:{row.revision}"
        return any(str(r.get("target_id")) in {want, any_want} and r.get("action") == "pass_slot" for r in records)

    async def build_context(self, row) -> GateContext:
        """装配 GateContext，并按需先跑 tagger 打标（payoff_density 的输入）。副作用：LLM + 写盘。"""
        ctx = self.ctx.gate_context(row.task_id, row.revision)
        if not ctx.payoff_tags:
            tags = await self._tag(row, ctx)
            if tags:
                ctx.payoff_tags = tags
                self._persist_tags(row.task_id, tags)
        return ctx

    async def _tag(self, row, ctx: GateContext) -> dict[str, list[str]]:
        """调用 tagger role 给每镜打标（输入仅 action+line，不含剧本上下文）。副作用：LLM 调用。"""
        payload = {
            "shots": [
                {"shot_id": s.shot_id, "action": s.action, "line": s.line or ""} for s in ctx.shots
            ],
            "tags": list(PAYOFF_TAGS),
        }
        try:
            client = self.ctx.llm("tagger")
            items: list[PayoffTagItem] = await client.agenerate_object(
                _system("tagger"), "请只输出每镜命中的爽点标签。" + _input_block(payload), list[PayoffTagItem], key="tagger"
            )
        except Exception as exc:  # noqa: BLE001 —— 打标失败时 payoff_density 记为无输入（不误判）
            logger.warning("tagger failed: {}", exc)
            return {}
        return {item.shot_id: list(item.tags) for item in items if item.tags}

    def _persist_tags(self, task_id: str, tags: dict[str, list[str]]) -> None:
        """把打标结果写回 constraints.json（供后续 slot 与报告复用）。副作用：写盘。"""
        paths = self.ctx.paths(task_id)
        raw = io.read_json(paths.constraints_json, default={}) or {}
        raw["payoff_tags"] = tags
        io.write_json(paths.constraints_json, raw)

    # ── 报告合并 ───────────────────────────────────────────
    def _merge_report(
        self,
        row,
        report: ConstraintReport,
        violations: list[Violation],
        constraints: list[ConstraintSpec],
        hints: list[str],
    ) -> None:
        """把本 slot 的判定合并进 constraint_report.json（五个 slot 各写一次）。副作用：写盘。"""
        paths = self.ctx.paths(row.task_id)
        existing = io.read_json(paths.constraint_report_json, default={}) or {}
        same_rev = int(existing.get("revision", -1)) == int(row.revision)
        verdicts = {v["constraint_id"]: v for v in existing.get("verdicts", [])} if same_rev else {}
        for v in report.verdicts:
            verdicts[v.constraint_id] = v.model_dump()
        order = {c.constraint_id: i for i, c in enumerate(constraints)}
        merged = sorted(
            verdicts.values(),
            key=lambda v: (order.get(v["constraint_id"], 10_000), v["constraint_id"]),
        )
        hard_failures = sum(1 for v in merged if v["verdict"] == "FAIL" and v["severity"] == "hard")
        needs_human = sum(1 for v in merged if v["verdict"] == "NEEDS_HUMAN")
        payload = {
            "task_id": row.task_id,
            "revision": row.revision,
            "spec_version": spec_version(merged),
            "verdicts": merged,
            "passed": hard_failures == 0 and needs_human == 0,
            "hard_failures": hard_failures,
            "needs_human": needs_human,
            "generated_at": report.generated_at,
            "slots_done": sorted({v["slot_key"] for v in merged}),
            "review_hints": existing.get("review_hints", []) or hints,
            "violations": [v.model_dump() for v in violations],
        }
        io.write_json(paths.constraint_report_json, payload)

    async def _review_hints(self, row, report: ConstraintReport, violations: list[Violation]) -> list[str]:
        """review role 的人工提示语（不参与判定）；LLM 不可用时退回确定性文案。副作用：LLM 调用。"""
        fallback = [
            f"{v.constraint_id}（{v.slot_key}/{v.validator_key}）判定异常，请人工确认是否可接受。"
            for v in violations[:5]
        ] or ["本段位无违规项；建议人工抽查首镜与集末钩子的可看性。"]
        payload = {
            "task_id": row.task_id,
            "slot_key": row.slot_key,
            "violations": [v.model_dump() for v in violations[:10]],
            "verdicts": [v.model_dump() for v in report.verdicts[:20]],
        }
        try:
            client = self.ctx.llm("review")
            hints = await client.agenerate_object(
                _system("review"),
                "请给出 3 条以内的人工复核提示语（只用于提示，不做判定）。" + _input_block(payload),
                list[str],
                key="review",
            )
            return [str(h)[:160] for h in (hints or [])] or fallback
        except Exception as exc:  # noqa: BLE001 —— 提示语不是判定输入，失败不影响闸门
            logger.debug("review hints unavailable: {}", exc)
            return fallback
