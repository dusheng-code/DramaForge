"""职责：交付包各章节的 markdown 片段构造（不判内容对错，只负责排版与字段对齐）。
输入：shots / packs / characters / constraints / 报告字典。
输出：markdown 字符串片段，由 assemble/assembler.py 拼装。

副作用：无。纯字符串处理。
"""

from __future__ import annotations

from typing import Any, Iterable

from ...contracts.artifacts import Character, ConstraintSpec, PromptPack, Shot
from ...contracts.enums import SLOT_KEYS
from ...io import md as mdlib

SLOT_TITLES: dict[str, str] = {
    "premise": "一、立意与前提",
    "structure": "二、结构与节奏",
    "emotion": "三、情绪曲线",
    "platform_voice": "四、平台语感",
    "risk": "五、合规与风险",
}


def cover(task: dict, counts: dict[str, int], rework: int) -> str:
    """封面区：题材、平台、集数、镜数、返工次数、生成时间。无副作用。"""
    return mdlib.join(
        [
            mdlib.h(1, f"DramaForge 交付包 · {task.get('topic', '')}"),
            mdlib.table(
                ("字段", "值"),
                [
                    ("task_id", task.get("task_id")),
                    ("平台", task.get("platform")),
                    ("集数", task.get("episode_count")),
                    ("单集目标时长(s)", task.get("episode_duration_target")),
                    ("严格模式", "是" if task.get("strict_mode") else "否"),
                    ("总镜数", counts.get("shots")),
                    ("返工次数(revision)", rework),
                    ("预算上限(¥)", task.get("cost_cap_cny")),
                    ("生成时间", task.get("generated_at")),
                ],
            ),
        ]
    )


def chapters_section(chapters: list[dict]) -> str:
    """五段位章节（五份必须齐全，空段位显式记 gaps）。无副作用。"""
    parts = [mdlib.h(2, "零、五段位章节")]
    by_slot = {c.get("slot_key"): c for c in chapters}
    for slot in SLOT_KEYS:
        ch = by_slot.get(slot, {})
        claims = ch.get("claims", []) or []
        rows = [(c.get("statement", ""), f"{float(c.get('confidence', 0)):.2f}", ", ".join(c.get("basis_ids", []) or []) or "—") for c in claims]
        parts.append(
            mdlib.section(
                SLOT_TITLES.get(slot, slot),
                mdlib.join(
                    [
                        str(ch.get("summary") or "（本段位无章节内容，见 gaps）"),
                        mdlib.table(("可核对断言", "置信度", "证据"), rows) if rows else "（无断言）",
                        "gaps: " + ("; ".join(ch.get("gaps", []) or []) or "无"),
                    ]
                ),
                level=3,
            )
        )
    return mdlib.join(parts)


def constraints_section(constraints: list[ConstraintSpec], report: dict) -> str:
    """约束清单 + 机读判定表。无副作用。"""
    verdict_by_id = {v.get("constraint_id"): v for v in (report.get("verdicts") or [])}
    rows = []
    for c in constraints:
        v = verdict_by_id.get(c.constraint_id, {})
        rows.append(
            (
                c.constraint_id,
                c.slot_key,
                c.validator_key,
                "硬" if c.hard else "软",
                str(c.value)[:40],
                c.fail_action,
                v.get("verdict", "未判定"),
            )
        )
    return mdlib.join(
        [
            mdlib.h(2, "一、约束清单与判定"),
            mdlib.table(("id", "段位", "校验器", "强度", "值", "失败动作", "判定"), rows),
            f"\n汇总：约束 {len(constraints)} 条，hard 失败 {report.get('hard_failures', 0)} 条，"
            f"待人工 {report.get('needs_human', 0)} 条，整体 {'通过' if report.get('passed') else '未通过'}。",
        ]
    )


def episodes_section(
    outline_episodes: Iterable[dict], blocks: list[dict], shots: list[Shot]
) -> str:
    """分集大纲 + 逐场逐镜表（机读字段全量列出）。无副作用。"""
    by_ep: dict[int, list[Shot]] = {}
    for s in shots:
        by_ep.setdefault(s.episode, []).append(s)
    parts = [mdlib.h(2, "二、分集大纲与逐场逐镜表")]
    for ep in outline_episodes:
        n = int(ep.get("episode") or 0)
        parts.append(mdlib.h(3, f"第 {n} 集 {ep.get('title', '')}"))
        parts.append(f"**本集目标**：{ep.get('goal', '')}")
        parts.append(f"**钩子**：{ep.get('hook', '')}")
        beats = "；".join(ep.get("beats", []) or [])
        if beats:
            parts.append(f"**节拍**：{beats}")
        rows = []
        for shot in by_ep.get(n, []):
            rows.append(
                (
                    shot.shot_id,
                    shot.scene,
                    f"{shot.duration_sec:g}",
                    shot.camera,
                    shot.action,
                    shot.line or "—",
                    shot.dialogue_speaker or "—",
                    shot.closing_state,
                    "hero" if shot.quality_tier == "hero" else "fast",
                    ",".join(shot.characters) or "—",
                )
            )
        parts.append(
            mdlib.table(
                ("shot_id", "场", "时长", "相机", "动作", "台词", "说话人", "收束", "档位", "角色"),
                rows,
            )
        )
        ep_blocks = [b for b in blocks if int(b.get("episode") or 0) == n]
        if ep_blocks:
            parts.append(mdlib.table(("block", "场", "摘要"), [(b.get("block_id"), b.get("scene"), b.get("summary")) for b in ep_blocks]))
    return mdlib.join(parts)


def prompt_pack_section(packs: list[PromptPack]) -> str:
    """提示词包：逐镜 image / motion / tts。无副作用。"""
    rows = [
        (
            p.shot_id,
            p.tier,
            p.resolution,
            f"{p.duration_sec:g}",
            p.image_prompt.replace("\n", " ⏎ "),
            p.motion_prompt,
            p.tts_text or "—",
        )
        for p in packs
    ]
    return mdlib.join(
        [mdlib.h(2, "三、逐镜提示词包"), mdlib.table(("shot_id", "档位", "分辨率", "时长", "image_prompt", "motion_prompt", "tts_text"), rows)]
    )


def assets_section(report: dict, assets: list[dict]) -> str:
    """资产清单与花费统计（含放弃清单与 attempt 直方图）。无副作用。"""
    rows = [
        (
            a.get("shot_id"),
            a.get("kind"),
            a.get("status"),
            a.get("attempt"),
            f"{float(a.get('cost_cny') or 0):.3f}",
            a.get("file_path") or "—",
        )
        for a in assets
    ]
    hist = report.get("attempt_hist") or {}
    return mdlib.join(
        [
            mdlib.h(2, "四、资产清单与花费"),
            f"总花费 ¥{float(report.get('total_spent_cny') or 0):.3f}；"
            f"整体通过率 {float(report.get('pass_rate') or 0):.2%}；"
            f"放弃镜数 {len(report.get('abandoned') or [])}。",
            mdlib.table(("shot_id", "类型", "状态", "attempt", "花费(¥)", "文件"), rows),
            mdlib.h(3, "attempt 分布"),
            mdlib.table(("attempt", "镜数"), sorted((k, v) for k, v in hist.items())),
            mdlib.h(3, "放弃清单"),
            mdlib.bullets(report.get("abandoned") or []) or "（无放弃）",
        ]
    )


def characters_section(characters: list[Character]) -> str:
    """角色视觉锚表（复现锚定用）。无副作用。"""
    rows = [
        (
            c.character_id,
            c.display_name,
            c.role,
            c.anchor_text,
            c.catchphrase or "—",
            (c.visual_anchor.ref_image_path or "—"),
        )
        for c in characters
    ]
    return mdlib.join([mdlib.h(2, "五、角色视觉锚"), mdlib.table(("id", "名字", "定位", "锚文本", "口头禅", "定妆图"), rows)])


def gaps_section(gaps: list[str]) -> str:
    """未覆盖项与人工待办。无副作用。"""
    return mdlib.join([mdlib.h(2, "六、未覆盖与待人工"), mdlib.bullets(gaps) or "（无）"])


def budget_section(quote: dict, spent: float, cap: float) -> str:
    """报价 vs 实际花费对照（§10.5）。无副作用。"""
    est = float(quote.get("est_total_cny") or 0)
    delta = spent - est
    pct = (abs(delta) / est * 100) if est else 0.0
    return mdlib.join(
        [
            mdlib.h(2, "七、成本核对"),
            mdlib.table(
                ("项", "值"),
                [
                    ("报价预估(¥)", f"{est:.3f}"),
                    ("实际花费(¥)", f"{spent:.3f}"),
                    ("差额(¥)", f"{delta:.3f}"),
                    ("偏差率", f"{pct:.2f}%"),
                    ("预算上限(¥)", f"{cap:.3f}"),
                    ("偏差是否超 5%", "是（需人工核对 provider 账单）" if pct > 5 else "否"),
                ],
            ),
        ]
    )
