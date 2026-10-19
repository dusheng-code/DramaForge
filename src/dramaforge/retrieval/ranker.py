"""职责：检索与校验共用的纯确定性计算：关键词抽取（SQL 降级检索用）、标签分布 L1 距离（payoff_density 校验器）、分布归一化、榜单热度摘要。
输入：任意中文文本、标签分布 dict、`platform_rank_snapshot` 行 dict 列表。
输出：关键词列表、L1 距离、归一化分布、可直接落 `rank_stats.json` 的摘要 dict。

副作用：无。全部为纯函数，不做 IO、不读配置、不写日志。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from decimal import Decimal
from typing import Any, Iterable

# 关键词抽取上限（SQL LIKE 检索每多一个词就多一轮全表条件，8 个足够）
DEFAULT_KEYWORD_TOP_N: int = 8
# 中文片段生成的 n-gram 长度区间
MIN_GRAM: int = 2
MAX_GRAM: int = 4
# 热度摘要里保留的 Top 条数（与 sql_repo.rank_stats 的默认 top_n 对齐）
HEAT_TOP_N: int = 20

_TOKEN_RE = re.compile(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)

# 单字停用词：中文片段里若整段只由这些字组成，则丢弃该 gram
_STOP_CHARS: frozenset[str] = frozenset(
    "的了是在和就都你我他她它这那有不也又很要会说着过把被让给对从向于为以之与及而则并或个些么呢吧啊哦嗯呀还再才只但而且所以"
)
# 整词停用表（少量高频虚词与检索无意义的词）
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "一个",
        "这个",
        "那个",
        "我们",
        "你们",
        "他们",
        "自己",
        "什么",
        "怎么",
        "不是",
        "就是",
        "可以",
        "然后",
        "因为",
        "所以",
        "但是",
        "如果",
        "已经",
        "还是",
        "这样",
        "那样",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
    }
)


def rank_keywords(text: str, top_n: int = DEFAULT_KEYWORD_TOP_N) -> list[str]:
    """确定性关键词抽取（无分词依赖）：ASCII 词 + 中文 2~4 gram，按词频/长度/字典序排序。

    用于 Milvus 不可用时的 SQL LIKE 检索（§3.1）：只做字面匹配，不做语义解析。
    无副作用。
    """
    if not text or top_n <= 0:
        return []
    normalized = _PUNCT_RE.sub(" ", str(text).lower())
    counter: Counter[str] = Counter()
    for token in _TOKEN_RE.findall(normalized):
        if _CJK_RE.match(token):
            counter.update(_cjk_grams(token))
        elif len(token) >= 2:
            counter[token] += 1
    ranked = [
        (term, count)
        for term, count in counter.items()
        if term not in _STOP_WORDS and not _only_stop_chars(term)
    ]
    ranked.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [term for term, _ in ranked[:top_n]]


def _cjk_grams(run: str) -> list[str]:
    """中文连续片段 → 2~4 元 gram 列表（片段不足 2 字时返回整段）。无副作用。"""
    length = len(run)
    if length < MIN_GRAM:
        return [run]
    grams: list[str] = []
    for size in range(MIN_GRAM, min(MAX_GRAM, length) + 1):
        for start in range(0, length - size + 1):
            grams.append(run[start : start + size])
    return grams


def _only_stop_chars(term: str) -> bool:
    """判断词是否完全由停用单字组成。无副作用。"""
    return bool(term) and all(ch in _STOP_CHARS for ch in term)


def normalize(dist: dict[str, float]) -> dict[str, float]:
    """把计数/权重分布归一化为和为 1 的分布；总和为 0 或非有限时返回空 dict。无副作用。"""
    if not dist:
        return {}
    total = 0.0
    for value in dist.values():
        number = _num(value)
        if number > 0:
            total += number
    if not math.isfinite(total) or total <= 0:
        return {}
    return {key: _num(value) / total for key, value in dist.items() if _num(value) > 0}


def l1_distance(p: dict[str, float], q: dict[str, float]) -> float:
    """两个分布（不要求已归一化）的 L1 距离 `Σ|p_i - q_i|`；缺失键按 0 处理。无副作用。

    `payoff_density` 校验器用它比较本轮标签频次分布与参照分布（标签集是 `contracts.enums.PAYOFF_TAGS`
    的封闭枚举，两边的键集必须落在该枚举内）：两者都先经 `normalize`，因此距离落在 `[0, 2]`。
    """
    keys = sorted(set(p) | set(q))
    return sum(abs(_num(p.get(key, 0.0)) - _num(q.get(key, 0.0))) for key in keys)


def heat_summary(rows: Iterable[dict]) -> dict:
    """`platform_rank_snapshot` 行列表 → 热度摘要（键名与 `sql_repo.rank_stats` 一致）。无副作用。

    输出：`total`、`platforms`、`avg_episode_count`、`avg_duration_sec`、`top`（按 heat_score 降序）。
    """
    records = [dict(row) for row in rows or []]
    platforms: Counter[str] = Counter()
    episode_counts: list[float] = []
    durations: list[float] = []
    for row in records:
        platform = row.get("platform")
        if platform:
            platforms[str(platform)] += 1
        episode = _num_or_none(row.get("episode_count"))
        if episode is not None:
            episode_counts.append(episode)
        duration = _num_or_none(row.get("avg_duration_sec"))
        if duration is not None:
            durations.append(duration)
    ordered = sorted(
        records,
        key=lambda row: (
            _num_or_none(row.get("heat_score")) is None,
            -(_num_or_none(row.get("heat_score")) or 0.0),
            str(row.get("title") or ""),
        ),
    )
    return {
        "total": len(records),
        "platforms": dict(platforms.most_common()),
        "avg_episode_count": _mean(episode_counts),
        "avg_duration_sec": _mean(durations),
        "top": [
            {
                "title": row.get("title"),
                "platform": row.get("platform"),
                "heat_score": _num_or_none(row.get("heat_score")),
                "episode_count": _int_or_none(row.get("episode_count")),
                "avg_duration_sec": _num_or_none(row.get("avg_duration_sec")),
                "genre_tags": row.get("genre_tags"),
            }
            for row in ordered[:HEAT_TOP_N]
        ],
        "source": "platform_rank_snapshot",
    }


def _mean(values: list[float]) -> float | None:
    """均值（保留两位）；空列表返回 None。无副作用。"""
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _num(value: Any) -> float:
    """任意数值 → float，非法值按 0 处理（负值按 0 处理，分布场景无负权重）。无副作用。"""
    number = _num_or_none(value)
    return number if number is not None and number > 0 else 0.0


def _num_or_none(value: Any) -> float | None:
    """任意数值 → float 或 None。无副作用。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    """任意数值 → int 或 None。无副作用。"""
    number = _num_or_none(value)
    return None if number is None else int(number)
