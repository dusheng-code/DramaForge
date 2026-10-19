"""职责：报价闸（§10.1）：prompt_pack 之后、任何花钱动作之前产出 review/cost_quote.json（只算账不花钱）。
输入：shots.json / packs.json / characters.json、TIER_PRICE、IMAGE_PRICE_CNY、任务与全局预算上限。
输出：CostQuote（逐镜逐类型明细 + 汇总 + 是否超上限）。

副作用：写磁盘、发 COST_QUOTE_READY 事件、写日志。禁止在此调用任何 provider。
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger

from ..contracts.artifacts import AssetQuoteItem, Character, CostQuote, PromptPack, Shot
from ..contracts.enums import Resolution
from ..contracts.ids import spec_version
from ..core.config import Settings
from ..io import artifacts as io


def _now() -> str:
    """本地时区 ISO 时间。无副作用。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_quote(
    task_id: str,
    revision: int,
    shots: list[Shot],
    packs: list[PromptPack],
    characters: list[Character],
    settings: Settings,
    cap_cny: float,
    expected_attempts: float = 1.0,
) -> CostQuote:
    """按镜头与角色生成报价单（anchor 一次 + 每镜 keyframe/video）。无副作用。"""
    from ..providers.pricing import est_anchor_cost, est_keyframe_cost, est_video_cost

    packs_by_shot = {p.shot_id: p for p in packs}
    items: list[AssetQuoteItem] = []
    anchor_cost = est_anchor_cost(settings)
    for char in characters:
        items.append(
            AssetQuoteItem(
                shot_id=char.character_id,
                kind="anchor_image",
                tier="fast",
                model=settings.image_model,
                resolution=settings.image_resolution,  # type: ignore[arg-type]
                duration_sec=0.0,
                unit_price_cny=round(anchor_cost, 4),
                expected_attempts=expected_attempts,
                est_cost_cny=round(anchor_cost * expected_attempts, 3),
            )
        )
    image_cost = est_keyframe_cost(settings)
    per_episode: dict[str, float] = {}
    for shot in shots:
        pack = packs_by_shot.get(shot.shot_id)
        tier = (pack.tier if pack else shot.quality_tier) or "fast"
        resolution: Resolution = (pack.resolution if pack else settings.video_resolution) or "768p"  # type: ignore[assignment]
        keyframe = AssetQuoteItem(
            shot_id=shot.shot_id,
            kind="keyframe",
            tier=tier,
            model=settings.image_model,
            resolution=resolution,
            duration_sec=shot.duration_sec,
            unit_price_cny=round(image_cost, 4),
            expected_attempts=expected_attempts,
            est_cost_cny=round(image_cost * expected_attempts, 3),
        )
        unit_price = settings.video_price(tier)
        video_cost = est_video_cost(tier, shot.duration_sec, settings, expected_attempts)
        video = AssetQuoteItem(
            shot_id=shot.shot_id,
            kind="video",
            tier=tier,
            model=settings.video_model(tier),
            resolution=resolution,
            duration_sec=shot.duration_sec,
            unit_price_cny=round(unit_price, 4),
            expected_attempts=expected_attempts,
            est_cost_cny=video_cost,
        )
        items.extend([keyframe, video])
        ep_key = str(shot.episode)
        per_episode[ep_key] = round(
            per_episode.get(ep_key, 0.0) + keyframe.est_cost_cny + video.est_cost_cny, 3
        )

    image_total = round(sum(i.est_cost_cny for i in items if i.kind != "video"), 3)
    video_total = round(sum(i.est_cost_cny for i in items if i.kind == "video"), 3)
    total = round(image_total + video_total, 3)
    quote = CostQuote(
        task_id=task_id,
        revision=revision,
        items=items,
        est_total_cny=total,
        image_total_cny=image_total,
        video_total_cny=video_total,
        per_episode_cny=per_episode,
        cap_cny=round(cap_cny, 3),
        within_cap=total <= cap_cny,
        approved=False,
        created_at=_now(),
    )
    return quote.model_copy(update={"spec_version": spec_version(quote.model_dump(exclude={"spec_version"}))})


def write_quote(quote: CostQuote, paths: io.TaskPaths) -> str:
    """把报价单落 review/cost_quote.json。副作用：写磁盘。"""
    io.write_json(paths.cost_quote_json, quote.model_dump())
    logger.info(
        "quote ready: task={} items={} total={} cap={} within={}",
        quote.task_id,
        len(quote.items),
        quote.est_total_cny,
        quote.cap_cny,
        quote.within_cap,
    )
    return str(paths.cost_quote_json)


def read_quote(paths: io.TaskPaths) -> CostQuote | None:
    """读取已落盘报价单。副作用：读磁盘。"""
    raw = io.read_json(paths.cost_quote_json, default=None)
    if not raw:
        return None
    try:
        return CostQuote.model_validate(raw)
    except Exception:  # noqa: BLE001 —— 脏报价单视为不存在，重新报价
        return None


def quote_by_shot(quote: CostQuote) -> dict[str, dict[str, float]]:
    """shot_id → {kind: est_cost}，供资产行落 est_cost_cny。无副作用。"""
    out: dict[str, dict[str, float]] = {}
    for item in quote.items:
        out.setdefault(item.shot_id, {})[item.kind] = float(item.est_cost_cny)
    return out


def episode_overflow(quote: CostQuote, episode_cap_cny: float) -> list[int]:
    """返回预估超单集上限的集号（供人工提前决策，不阻断）。无副作用。"""
    out: list[int] = []
    for ep, cost in quote.per_episode_cny.items():
        try:
            if float(cost) > episode_cap_cny:
                out.append(int(ep))
        except (TypeError, ValueError):
            continue
    return sorted(out)
