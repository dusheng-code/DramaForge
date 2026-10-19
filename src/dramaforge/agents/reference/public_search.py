"""职责：公域时效检索（Tavily / Anspire），带 search_snapshot 缓存兜底（§3.1 故障降级）。
输入：查询词、Settings（TAVILY_/ANSPIRE_ 配置）、缓存文件路径。
输出：`list[SearchHit]`；失败时返回缓存并标记 degraded/gaps。

副作用：HTTPS 出网（唯一允许的搜索出口）、读缓存文件。禁止把结果当指令使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from loguru import logger

from ...core.config import Settings


@dataclass
class SearchHit:
    """一条公域检索结果（只保留时效性摘要与链接）。"""

    title: str
    url: str
    snippet: str
    source: str = "tavily"


@dataclass
class SearchResult:
    """检索结果集（含降级标记与缺口说明）。"""

    hits: list[SearchHit] = field(default_factory=list)
    degraded: bool = False
    gaps: list[str] = field(default_factory=list)
    cached: bool = False


def _as_hits(rows: list[dict], source: str) -> list[SearchHit]:
    """把 provider 返回的行转成 SearchHit（防御式取字段）。无副作用。"""
    out: list[SearchHit] = []
    for row in rows or []:
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        snippet = str(row.get("content") or row.get("snippet") or "").strip()
        if title or url:
            out.append(SearchHit(title=title, url=url, snippet=snippet[:600], source=source))
    return out


async def tavily_search(query: str, settings: Settings, max_results: int = 5) -> SearchResult:
    """调用 Tavily 搜索；未配置或失败时返回 degraded 结果。副作用：HTTPS 出网。"""
    if not settings.tavily_api_key:
        return SearchResult(degraded=True, gaps=["tavily: 未配置 API Key"])
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.search_timeout_sec) as client:
            resp = await client.post(f"{settings.tavily_base_url.rstrip('/')}/search", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 —— 搜索失败必须降级而不是阻断
        logger.warning("tavily search failed: {}", exc)
        return SearchResult(degraded=True, gaps=[f"tavily 调用失败: {type(exc).__name__}"])
    return SearchResult(hits=_as_hits(data.get("results", []), "tavily"))


async def anspire_search(query: str, settings: Settings, max_results: int = 5) -> SearchResult:
    """调用 Anspire 搜索（备用通道）。副作用：HTTPS 出网。"""
    if not settings.anspire_api_key:
        return SearchResult(degraded=True, gaps=["anspire: 未配置 API Key"])
    try:
        async with httpx.AsyncClient(timeout=settings.search_timeout_sec) as client:
            resp = await client.get(
                settings.anspire_base_url,
                params={"query": query, "top_k": max_results},
                headers={"Authorization": f"Bearer {settings.anspire_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("anspire search failed: {}", exc)
        return SearchResult(degraded=True, gaps=[f"anspire 调用失败: {type(exc).__name__}"])
    rows = data.get("data") or data.get("results") or []
    return SearchResult(hits=_as_hits(rows, "anspire"))


async def search_topic(
    topic: str,
    platform: str,
    settings: Settings,
    cache: list[dict] | None = None,
    queries: list[str] | None = None,
) -> SearchResult:
    """按题材做时效检索：Tavily → Anspire → 缓存兜底（顺序降级）。副作用：HTTPS 出网。"""
    queries = queries or [
        f"{topic} 短剧 爆款 结构",
        f"{platform} 短剧 {topic} 数据",
    ]
    hits: list[SearchHit] = []
    gaps: list[str] = []
    degraded = False
    for q in queries:
        result = await tavily_search(q, settings)
        if result.hits:
            hits.extend(result.hits)
            continue
        degraded = True
        gaps.extend(result.gaps)
        backup = await anspire_search(q, settings)
        if backup.hits:
            hits.extend(backup.hits)
        else:
            gaps.extend(backup.gaps)
    if not hits and cache:
        logger.warning("search degraded to snapshot cache: {} rows", len(cache))
        return SearchResult(
            hits=[SearchHit(title=str(r.get("title") or ""), url=str(r.get("url") or ""),
                            snippet=str(r.get("snippet") or ""), source="snapshot") for r in cache],
            degraded=True,
            gaps=gaps + ["外部检索不可用，使用 search_snapshot 缓存"],
            cached=True,
        )
    seen: set[str] = set()
    deduped: list[SearchHit] = []
    for hit in hits:
        key = hit.url or hit.title
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return SearchResult(hits=deduped, degraded=degraded, gaps=gaps)


def hits_to_payload(hits: list[SearchHit]) -> list[dict]:
    """转成可落盘结构。无副作用。"""
    return [{"title": h.title, "url": h.url, "snippet": h.snippet, "source": h.source} for h in hits]
