"""职责：§9.2 quality_tier 确定性赋档与降级：hero 三条件命中即 hero，hero 秒数占比超限时按优先级降级。
输入：Shot 列表（已含 episode/scene/line/characters/closing_state/is_turn）、HERO_MAX_RATIO。
输出：赋档后的 Shot 列表（原地返回新对象）与降级日志摘要。

副作用：只写 info 日志（含 shot_id 与降级原因）。禁止 LLM 参与赋档（§8 明确要求）。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from ...contracts.artifacts import Shot

# 降级优先级：数字越小越先被降级（末镜最难被降级，保证集末钩子质感）
PRIORITY_LINE = 0  # 台词镜
PRIORITY_TURN = 1  # 反转镜
PRIORITY_LAST = 2  # 末镜

_TIER_RANK = {"fast": 0, "hero": 1}


@dataclass(frozen=True)
class TierDecision:
    """单个镜头的赋档结论（供日志与测试断言）。"""

    shot_id: str
    tier: str
    reason: str


def is_hero_candidate(shot: Shot, episode_shot_count: int, index_in_episode: int) -> tuple[bool, str]:
    """§9.2 三条件判据。返回 (是否 hero, 原因)。无副作用。"""
    is_last = index_in_episode == episode_shot_count - 1
    if is_last and shot.closing_state != "resolved":
        return True, "末镜且未收束"
    if shot.is_turn:
        return True, "is_turn"
    if shot.line and shot.characters:
        return True, "有台词且有角色"
    return False, ""


def _priority(reason: str) -> int:
    """把 hero 原因映射成降级优先级。无副作用。"""
    if reason.startswith("末镜"):
        return PRIORITY_LAST
    if reason == "is_turn":
        return PRIORITY_TURN
    return PRIORITY_LINE


def assign_tiers(shots: list[Shot], hero_max_ratio: float) -> tuple[list[Shot], list[TierDecision]]:
    """给整批镜头赋档；hero 秒数占比超限时按优先级降级。无副作用（返回新列表）。"""
    if not shots:
        return [], []
    by_episode: dict[int, list[Shot]] = {}
    for shot in shots:
        by_episode.setdefault(shot.episode, []).append(shot)

    decisions: list[TierDecision] = []
    result: dict[str, Shot] = {}
    candidates: list[tuple[int, Shot, str]] = []

    for ep, ep_shots in by_episode.items():
        for idx, shot in enumerate(ep_shots):
            hero, reason = is_hero_candidate(shot, len(ep_shots), idx)
            tier = "hero" if hero else "fast"
            result[shot.shot_id] = shot.model_copy(update={"quality_tier": tier})
            if hero:
                candidates.append((_priority(reason), shot, reason))
            decisions.append(TierDecision(shot_id=shot.shot_id, tier=tier, reason=reason or "非 hero 条件"))

    total_sec = sum(s.duration_sec for s in shots) or 1e-9
    hero_sec = sum(s.duration_sec for s in shots if result[s.shot_id].quality_tier == "hero")
    if hero_sec / total_sec > hero_max_ratio:
        # 从低优先级开始降级，直到占比达标或无可降
        for priority, shot, reason in sorted(candidates, key=lambda x: x[0]):
            if hero_sec / total_sec <= hero_max_ratio:
                break
            updated = result[shot.shot_id].model_copy(update={"quality_tier": "fast"})
            result[shot.shot_id] = updated
            hero_sec -= shot.duration_sec
            logger.info(
                "tier downgraded hero→fast: shot_id={} reason={} (hero_ratio>{:.2f})",
                shot.shot_id,
                reason,
                hero_max_ratio,
            )
            decisions.append(
                TierDecision(
                    shot_id=shot.shot_id,
                    tier="fast",
                    reason=f"hero 占比超限降级（原条件：{reason}）",
                )
            )
    ordered = [result[s.shot_id] for s in shots]
    return ordered, decisions


def hero_ratio(shots: list[Shot]) -> float:
    """hero 档位的秒数占比（用于报告与断言）。无副作用。"""
    total = sum(s.duration_sec for s in shots)
    if total <= 0:
        return 0.0
    hero = sum(s.duration_sec for s in shots if s.quality_tier == "hero")
    return hero / total
