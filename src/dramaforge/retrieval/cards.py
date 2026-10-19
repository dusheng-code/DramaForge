"""职责：参照卡检索的编排层：先走 Milvus 双路混合检索，失败或空命中时降级到 `reference_card` 的 SQL 关键词检索（§3.1），并统一成 CardBundle。
输入：task_id、检索 query、Settings、可选 slot 过滤；可注入的 MilvusCardRepo / ReferenceCardRepo（测试用 Fake）。
输出：CardBundle（cards / degraded / source / gaps）；cards_to_payload 产出可写 `reference/cards_used.json` 的形状。

副作用：发 Milvus 检索、读写 MySQL（降级路径）、写 warn/info 日志；不写磁盘（落盘由 agents/io 负责）。
"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel

from ..core.config import Settings, get_settings
from .milvus_repo import CardHit, EmbedderNotAvailable, MilvusCardRepo, MilvusUnavailable
from .ranker import rank_keywords
from .sql_repo import KEYWORD_SEARCH_OVERFETCH, ReferenceCardRepo

# 降级检索用的关键词数量（与 ranker 默认值对齐）
DEGRADED_KEYWORD_TOP_N: int = 8


class CardBundle(BaseModel):
    """一次参照卡检索的结果包：卡片本身 + 是否降级 + 数据来源 + 缺口说明。

    `cards` 故意声明为 `list[Any]`：Milvus 命中是 `CardHit`、SQL 降级命中是 dict；若写成
    `list[CardHit | dict]`，pydantic 智能联合会把 SQL 行强转成 CardHit 并丢掉 title/platform 等字段。
    """

    cards: list[Any] = []
    degraded: bool = False
    source: Literal["milvus", "sql"] = "milvus"
    gaps: list[str] = []


async def retrieve_cards(
    task_id: str,
    query: str,
    settings: Settings | None = None,
    *,
    slot_keys: list[str] | None = None,
    milvus: MilvusCardRepo | None = None,
    sql: ReferenceCardRepo | None = None,
) -> CardBundle:
    """检索参照卡：Milvus 优先，`MilvusUnavailable` / `EmbedderNotAvailable` 时降级到 SQL 关键词检索。

    §3.1：Milvus 挂掉时**记 warn 并继续链路**，绝不因为检索失败中断任务。
    返回值里 `degraded=True` 表示走了 SQL 兜底，`gaps` 记录降级原因与空命中原因。

    副作用：Milvus 检索（可能超时）、SQL 读库、warn/info 日志；若内部自建 SQL 仓库会负责关闭连接池。
    """
    s = settings or get_settings()
    gaps: list[str] = []
    top_k = int(s.card_top_k)

    try:
        hits = await (milvus or MilvusCardRepo(s)).hybrid_search(query, slot_keys, top_k)
    except (MilvusUnavailable, EmbedderNotAvailable) as exc:
        reason = f"{type(exc).__name__}：{exc}"
        logger.warning(
            "参照卡检索降级到 SQL 关键词检索（task_id={}，原因={}）", task_id, reason
        )
        gaps.append(f"Milvus 检索不可用，已降级到 SQL 关键词检索（{reason}）")
    else:
        cards = _pick(hits, top_k)
        if cards:
            return CardBundle(cards=cards, degraded=False, source="milvus", gaps=gaps)
        logger.warning("参照卡 Milvus 检索空命中，降级到 SQL 关键词检索（task_id={}）", task_id)
        gaps.append("Milvus 检索无命中，已降级到 SQL 关键词检索")

    keywords = rank_keywords(query, top_n=DEGRADED_KEYWORD_TOP_N)
    if not keywords:
        gaps.append("query 无法抽出关键词，SQL 降级检索跳过")
        return CardBundle(cards=[], degraded=True, source="sql", gaps=gaps)

    own_sql = sql is None
    repo = sql or ReferenceCardRepo.from_settings(s)
    try:
        rows = await repo.keyword_search(keywords, slot_keys, limit=top_k * KEYWORD_SEARCH_OVERFETCH)
    finally:
        if own_sql:
            await repo.close()
    cards = _pick(rows, top_k)
    if not cards:
        gaps.append(f"SQL 关键词检索无命中（keywords={len(keywords)}）")
    logger.info(
        "参照卡 SQL 降级检索完成（task_id={}，keywords={}，cards={}）", task_id, len(keywords), len(cards)
    )
    return CardBundle(cards=cards, degraded=True, source="sql", gaps=gaps)


def cards_to_payload(cards: list[Any] | CardBundle) -> list[dict]:
    """把 CardHit / SQL 行转换成 `reference/cards_used.json` 的形状（字段固定为七项 + source）。无副作用。

    `source` 区分来源（`milvus` / `sql`），供 `constraint` role 的 `source_refs` 追溯。
    """
    items = cards.cards if isinstance(cards, CardBundle) else list(cards or [])
    payload: list[dict] = []
    for card in items:
        getter = _getter(card)
        payload.append(
            {
                "card_id": getter("card_id"),
                "slot_key": getter("slot_key"),
                "assertion": getter("assertion") or getter("text") or "",
                "numeric_value": getter("numeric_value"),
                "numeric_unit": getter("numeric_unit"),
                "platform": getter("platform"),
                "source_url": getter("source_url"),
                "source": getter("source") or ("milvus" if isinstance(card, CardHit) else "sql"),
            }
        )
    return payload


def _pick(cards: list[Any], top_k: int) -> list[Any]:
    """按 slot 分组去重（同 card_id 只留首条），每 slot 最多 top_k 条，保持原顺序。无副作用。"""
    if not cards:
        return []
    seen: set[tuple[str, str]] = set()
    per_slot: dict[str, int] = {}
    picked: list[Any] = []
    for card in cards:
        getter = _getter(card)
        card_id = str(getter("card_id") or "")
        slot_key = str(getter("slot_key") or "")
        key = (card_id, slot_key)
        if not card_id or key in seen:
            continue
        if per_slot.get(slot_key, 0) >= top_k:
            continue
        seen.add(key)
        per_slot[slot_key] = per_slot.get(slot_key, 0) + 1
        picked.append(card)
    return picked


def _getter(card: Any):
    """返回统一取值函数：CardHit 走属性，dict 走 key。无副作用。"""

    def _get(name: str, default: Any = None) -> Any:
        """dict 取 key、CardHit 取属性，统一两种载体的取值口径。无副作用。"""
        if isinstance(card, dict):
            return card.get(name, default)
        return getattr(card, name, default)

    return _get
