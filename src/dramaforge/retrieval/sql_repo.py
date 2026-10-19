"""职责：`reference_card` 与 `platform_rank_snapshot` 的 SQL 唯一访存入口（§5.3），并提供 Milvus 不可用时的关键词检索降级路径（§3.1）。
输入：参考卡片行 dict、关键词列表、slot 过滤、AsyncEngine（DSN 由 Settings 注入）；ids 列表。
输出：卡片 dict 列表（含 hit_count）、写入条数、可直接落 `rank_stats.json` 的统计 dict、公域检索用标题列表。

副作用：读写 MySQL（sqlalchemy.ext.asyncio + text()）；不访问 Milvus、不写磁盘、不发外部网络。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from loguru import logger
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..core.config import Settings, get_settings

# 降级检索命中条数的默认放大倍数（先多取，再分 slot 去重取 top_k）
KEYWORD_SEARCH_OVERFETCH: int = 3
_HEAT_TOP_N: int = 20


class ReferenceCardRepo:
    """参照卡与榜单快照的 SQL 仓库。引擎由 connect()/from_settings() 创建，连接池自身重试。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    # ── 生命周期 ───────────────────────────────────────────
    @classmethod
    def connect(cls, dsn: str, echo: bool = False) -> "ReferenceCardRepo":
        """按 DSN 建异步引擎（pool_pre_ping + 回收）。无 IO（惰性连接）。"""
        engine = create_async_engine(
            dsn,
            echo=echo,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=5,
        )
        return cls(engine)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ReferenceCardRepo":
        """按 TASK_STATE_DB 建仓库（调用方负责 close）。无 IO（惰性连接）。"""
        s = settings or get_settings()
        return cls.connect(s.task_state_db)

    async def close(self) -> None:
        """释放连接池。副作用：关闭连接。"""
        await self.engine.dispose()

    # ── reference_card 写 ──────────────────────────────────
    async def upsert_cards(self, rows: list[dict]) -> int:
        """按 (card_id, slot_key) 主键 upsert 参考卡片；返回**提交条数**（不是影响行数）。

        副作用：写库（一条多值 INSERT ... ON DUPLICATE KEY UPDATE）。
        """
        if not rows:
            return 0
        payloads = [_card_params(row) for row in rows]
        sql = text(
            """
            INSERT INTO reference_card
                (card_id, slot_key, title, platform, assertion, numeric_value, numeric_unit,
                 payload, annotator, source_url, annotated_at)
            VALUES
                (:card_id, :slot_key, :title, :platform, :assertion, :numeric_value, :numeric_unit,
                 :payload, :annotator, :source_url, :annotated_at)
            ON DUPLICATE KEY UPDATE
                title=VALUES(title), platform=VALUES(platform), assertion=VALUES(assertion),
                numeric_value=VALUES(numeric_value), numeric_unit=VALUES(numeric_unit),
                payload=VALUES(payload), annotator=VALUES(annotator),
                source_url=VALUES(source_url), annotated_at=VALUES(annotated_at)
            """
        )
        async with self.engine.begin() as conn:
            await conn.execute(sql, payloads)
        logger.info("reference_card upsert: submitted={}", len(payloads))
        return len(payloads)

    # ── reference_card 读 ──────────────────────────────────
    async def all_cards(self, slot_key: str | None = None) -> list[dict]:
        """读取全部卡片（可按 slot 过滤），按 (slot_key, card_id) 排序。副作用：读库。"""
        if slot_key:
            sql = text(
                "SELECT * FROM reference_card WHERE slot_key=:slot ORDER BY slot_key, card_id"
            )
            params: dict[str, Any] = {"slot": slot_key}
        else:
            sql = text("SELECT * FROM reference_card ORDER BY slot_key, card_id")
            params = {}
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, params)
            return [card_row(row) for row in res.mappings().all()]

    async def by_ids(self, ids: Iterable[str]) -> list[dict]:
        """按 id 取卡片；id 可以是 `card_id` 或 `card_id:slot_key` 两种写法。

        副作用：读库（一条 IN 查询）。返回顺序与入参无关，按 (slot_key, card_id) 排序。
        """
        wanted = [str(item).strip() for item in ids if str(item).strip()]
        if not wanted:
            return []
        card_ids = {item.split(":", 1)[0] for item in wanted}
        pairs = {(cid, slot) for cid, _, slot in (item.partition(":") for item in wanted) if slot}
        sql = text("SELECT * FROM reference_card WHERE card_id IN :cids ORDER BY slot_key, card_id").bindparams(
            bindparam("cids", expanding=True)
        )
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, {"cids": sorted(card_ids)})
            rows = [card_row(row) for row in res.mappings().all()]
        if not pairs:
            return rows
        # 带 slot 的 id 只保留命中那一行；不带 slot 的 id 保留其全部 slot 行
        keep_pairs = pairs
        pure_ids = {item for item in wanted if ":" not in item}
        return [
            row
            for row in rows
            if (str(row.get("card_id")), str(row.get("slot_key"))) in keep_pairs
            or str(row.get("card_id")) in pure_ids
        ]

    async def keyword_search(
        self,
        keywords: list[str],
        slot_keys: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """关键词降级检索（§3.1 Milvus 挂掉时的兜底）：对 `assertion` 与 `title` 做 LIKE。

        排序：命中关键词数降序 → card_id 升序（确定性）。结果带 `hit_count` 字段。
        副作用：读库（一条 LIKE 查询）；关键词为空时直接返回空列表（不查库）。
        """
        terms = _clean_keywords(keywords)
        if not terms:
            logger.warning("keyword_search 收到空关键词，返回空结果（不降级成全表扫描）")
            return []
        score_parts: list[str] = []
        where_parts: list[str] = []
        params: dict[str, Any] = {"lim": int(max(1, limit))}
        for idx, term in enumerate(terms):
            name = f"kw{idx}"
            params[name] = _like_pattern(term)
            score_parts.append(f"(CASE WHEN assertion LIKE :{name} THEN 1 ELSE 0 END)")
            score_parts.append(f"(CASE WHEN title LIKE :{name} THEN 1 ELSE 0 END)")
            where_parts.append(f"assertion LIKE :{name}")
            where_parts.append(f"title LIKE :{name}")
        score_sql = " + ".join(score_parts)
        where_sql = " OR ".join(where_parts)
        slot_sql = ""
        cleaned_slots = [str(s) for s in (slot_keys or []) if str(s).strip()]
        if cleaned_slots:
            slot_sql = f" AND slot_key IN ({', '.join(f':slot{i}' for i in range(len(cleaned_slots)))})"
            for idx, slot in enumerate(cleaned_slots):
                params[f"slot{idx}"] = slot
        sql = text(
            f"""
            SELECT *, ({score_sql}) AS hit_count
            FROM reference_card
            WHERE ({where_sql}){slot_sql}
            ORDER BY hit_count DESC, card_id ASC
            LIMIT :lim
            """
        )
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, params)
            rows = [card_row(row) for row in res.mappings().all()]
        logger.info("keyword_search: terms={} hits={}", len(terms), len(rows))
        return rows

    # ── platform_rank_snapshot ─────────────────────────────
    async def rank_stats(self, top_n: int = _HEAT_TOP_N) -> dict:
        """聚合榜单快照 → 可直接落 `reference/rank_stats.json` 的 dict。

        含：条数 `total`、平台分布 `platforms`、平均集数、平均单集时长、快照日期区间、热度 Top N。
        键名与 `retrieval.ranker.heat_summary` 保持一致。副作用：读库（2~3 条聚合查询）。
        """
        async with self.engine.connect() as conn:
            agg = (
                await conn.execute(
                    text(
                        """
                        SELECT COUNT(*) AS total,
                               AVG(episode_count) AS avg_episode_count,
                               AVG(avg_duration_sec) AS avg_duration_sec,
                               MIN(snapshot_date) AS first_date,
                               MAX(snapshot_date) AS last_date
                        FROM platform_rank_snapshot
                        """
                    )
                )
            ).mappings().first() or {}
            platforms = (
                await conn.execute(
                    text(
                        """
                        SELECT platform, COUNT(*) AS cnt
                        FROM platform_rank_snapshot
                        WHERE platform IS NOT NULL
                        GROUP BY platform ORDER BY cnt DESC, platform ASC
                        """
                    )
                )
            ).mappings().all()
            top = (
                await conn.execute(
                    text(
                        """
                        SELECT title, platform, heat_score, episode_count,
                               avg_duration_sec, genre_tags, snapshot_date
                        FROM platform_rank_snapshot
                        WHERE title IS NOT NULL AND title <> ''
                        ORDER BY heat_score IS NULL, heat_score DESC, title ASC
                        LIMIT :lim
                        """
                    ),
                    {"lim": int(max(1, top_n))},
                )
            ).mappings().all()
        return {
            "total": int(agg.get("total") or 0),
            "platforms": {str(row["platform"]): int(row["cnt"] or 0) for row in platforms},
            "avg_episode_count": _round(agg.get("avg_episode_count")),
            "avg_duration_sec": _round(agg.get("avg_duration_sec")),
            "date_range": {
                "start": _iso(agg.get("first_date")),
                "end": _iso(agg.get("last_date")),
            },
            "top": [
                {
                    "title": row.get("title"),
                    "platform": row.get("platform"),
                    "heat_score": _round(row.get("heat_score")),
                    "episode_count": _int_or_none(row.get("episode_count")),
                    "avg_duration_sec": _round(row.get("avg_duration_sec")),
                    "genre_tags": row.get("genre_tags"),
                    "snapshot_date": _iso(row.get("snapshot_date")),
                }
                for row in top
            ],
            "source": "platform_rank_snapshot",
        }

    async def top_titles(self, limit: int = 50) -> list[str]:
        """取热度最高的剧名列表（供「公域时效检索」拼 query）。副作用：读库。"""
        sql = text(
            """
            SELECT title, MAX(heat_score) AS peak
            FROM platform_rank_snapshot
            WHERE title IS NOT NULL AND title <> ''
            GROUP BY title
            ORDER BY peak IS NULL, peak DESC, title ASC
            LIMIT :lim
            """
        )
        async with self.engine.connect() as conn:
            res = await conn.execute(sql, {"lim": int(max(1, limit))})
            return [str(row[0]) for row in res.all() if row[0]]


# ── 行转换与参数清洗（模块级，便于单测直接调用）──────────────
def card_row(mapping: Any) -> dict:
    """`reference_card` 行 → 可写 `cards_used.json` 的 dict（DECIMAL/DATE/JSON 已归一）。无副作用。"""
    m = dict(mapping)
    payload = _json_field(m.get("payload"))
    row: dict[str, Any] = {
        "card_id": m.get("card_id"),
        "slot_key": m.get("slot_key"),
        "title": m.get("title"),
        "platform": m.get("platform"),
        "assertion": m.get("assertion") or "",
        "numeric_value": _float_or_none(m.get("numeric_value")),
        "numeric_unit": m.get("numeric_unit"),
        "payload": payload,
        "annotator": m.get("annotator"),
        "source_url": m.get("source_url"),
        "annotated_at": _iso(m.get("annotated_at")),
        "source": "sql",
    }
    if "hit_count" in m:
        row["hit_count"] = _int_or_none(m.get("hit_count"))
    return row


def _card_params(row: dict) -> dict[str, Any]:
    """外部 dict → SQL 绑定参数（长度截断、JSON 序列化、日期归一）。无副作用。"""
    payload = row.get("payload")
    if payload is None:
        payload_json = json.dumps({}, ensure_ascii=False)
    elif isinstance(payload, str):
        payload_json = payload
    else:
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    card_id = str(row.get("card_id") or "").strip()
    slot_key = str(row.get("slot_key") or "").strip()
    if not card_id or not slot_key:
        raise ValueError("reference_card 行缺少 card_id 或 slot_key（主键列不可为空）")
    assertion = str(row.get("assertion") or "").strip()
    if not assertion:
        raise ValueError(f"reference_card 行 {card_id}:{slot_key} 的 assertion 不可为空（NOT NULL 列）")
    title = row.get("title")
    return {
        "card_id": card_id[:64],
        "slot_key": slot_key[:16],
        "title": (str(title)[:128] if title is not None else None),
        "platform": (str(row.get("platform"))[:32] if row.get("platform") is not None else None),
        "assertion": assertion[:512],
        "numeric_value": _float_or_none(row.get("numeric_value")),
        "numeric_unit": (str(row.get("numeric_unit"))[:16] if row.get("numeric_unit") is not None else None),
        "payload": payload_json,
        "annotator": (str(row.get("annotator"))[:32] if row.get("annotator") is not None else None),
        "source_url": (str(row.get("source_url"))[:512] if row.get("source_url") is not None else None),
        "annotated_at": _date_or_none(row.get("annotated_at")),
    }


def _clean_keywords(keywords: Iterable[str]) -> list[str]:
    """关键词清洗：去空、去重、按长度降序（长词更能命中），最多 8 个。无副作用。"""
    seen: dict[str, None] = {}
    for raw in keywords or []:
        term = str(raw or "").strip()
        if term:
            seen.setdefault(term, None)
    return sorted(seen, key=lambda item: (-len(item), item))[:8]


def _like_pattern(term: str) -> str:
    """把关键词转成 LIKE 模式：转义 `\\`、`%`、`_`，用 MySQL 默认反斜杠转义。无副作用。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _json_field(value: Any) -> dict[str, Any]:
    """JSON 列读取：aiomysql 返回 str，兼容已是 dict 的驱动。无副作用。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _float_or_none(value: Any) -> float | None:
    """DECIMAL/str/None → float 或 None（保持 NULL 语义）。无副作用。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    """任意数值 → int 或 None。无副作用。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int = 2) -> float | None:
    """数值 → 保留 digits 位的 float 或 None。无副作用。"""
    number = _float_or_none(value)
    return None if number is None else round(number, digits)


def _iso(value: Any) -> str | None:
    """date/datetime/str → ISO 字符串或 None（JSON 可序列化）。无副作用。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _date_or_none(value: Any) -> date | None:
    """str/date/datetime → date 或 None（MySQL DATE 列绑定）。无副作用。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
