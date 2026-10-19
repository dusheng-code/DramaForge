"""职责：Milvus collection `drama_card` 的唯一访存入口（§5.3）：建集合、dense+sparse 双路检索、RRF 融合、离线灌库。
输入：Settings（MILVUS_URI / MILVUS_COLLECTION / EMBED_MODEL / EMBED_DIM / RRF_K / CARD_TOP_K / CARD_MILVUS_TIMEOUT_SEC）、query 文本、CardDoc 列表。
输出：CardHit 列表（含单路名次）、写入条数、集合创建结果；Milvus 或嵌入模型不可用时抛 MilvusUnavailable / EmbedderNotAvailable 供上层降级。

副作用：发 Milvus gRPC 调用、惰性加载 BGE-M3（FlagEmbedding 未安装时只降级不崩）。本模块**不做任何 MySQL 访问**、不写磁盘。
"""

from __future__ import annotations

import asyncio
import importlib.util
from functools import lru_cache
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

from loguru import logger
from pymilvus import AnnSearchRequest, CollectionSchema, DataType, FieldSchema, MilvusClient, RRFRanker
from pydantic import BaseModel

from ..contracts.ids import card_milvus_id
from ..core.config import Settings, get_settings

# ── §5.3 collection 字段与索引口径 ─────────────────────────
ID_FIELD: str = "id"
SLOT_FIELD: str = "slot_key"
TEXT_FIELD: str = "text"
DENSE_FIELD: str = "dense_vector"
SPARSE_FIELD: str = "sparse_vector"

ID_MAX_LEN: int = 128
SLOT_MAX_LEN: int = 16
TEXT_MAX_LEN: int = 65535

DENSE_METRIC: str = "IP"
SPARSE_METRIC: str = "IP"
DENSE_INDEX_TYPE: str = "FLAT"
SPARSE_INDEX_TYPE: str = "SPARSE_INVERTED_INDEX"
SPARSE_INDEX_ALGO: str = "DAAT_MAXSCORE"

FLAGEMBEDDING_MODULE: str = "FlagEmbedding"


class MilvusUnavailable(RuntimeError):
    """Milvus 不可用（连不上、超时、返回异常）。上层据此降级到 SQL 关键词检索（§3.1）。"""


class EmbedderNotAvailable(RuntimeError):
    """向量化不可用（未安装 FlagEmbedding / 模型加载失败 / 维度与 EMBED_DIM 不一致）。"""


class CardHit(BaseModel):
    """一次混合检索命中的参照卡；`rank_dense` / `rank_sparse` 为单路名次（拿不到时为 None）。"""

    card_id: str
    slot_key: str
    text: str = ""
    score: float = 0.0
    rank: int = 0
    rank_dense: int | None = None
    rank_sparse: int | None = None
    source: Literal["milvus"] = "milvus"


class CardDoc(BaseModel):
    """离线灌库用的 Milvus 文档；向量缺省时由 Embedder 现算。`id` 由 card_id+slot_key 派生。"""

    card_id: str
    slot_key: str
    text: str
    dense_vector: list[float] | None = None
    sparse_vector: dict[int, float] | None = None


@runtime_checkable
class Embedder(Protocol):
    """向量化接口：dense 查询/文档向量 + sparse 词权重（BGE-M3 的 lexical weights）。"""

    model_name: str
    dim: int

    @property
    def available(self) -> bool:
        """依赖是否可用（FlagEmbedding 是否可 import）。无副作用。"""
        ...

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """查询侧 dense 向量（在线路径只对 query 单条做）。无副作用（除模型推理）。"""
        ...

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文档侧 dense 向量（离线灌库用）。无副作用（除模型推理）。"""
        ...

    def encode_sparse(self, texts: Sequence[str]) -> list[dict[int, float]]:
        """稀疏词权重向量（供 sparse_vector 字段）。无副作用（除模型推理）。"""
        ...


class BgeM3Embedder:
    """BGE-M3 的惰性封装：首次调用才 import FlagEmbedding 并加载模型（重依赖 torch）。

    约束（§3.1）：在线进程**禁止**批量向量化，只对单条 query 调 `encode_queries`；
    批量灌库走 `encode_documents`，由离线脚本调用。
    """

    def __init__(self, model_name: str = "BGE-M3", dim: int = 1024, use_fp16: bool = True) -> None:
        self.model_name = model_name
        self.dim = int(dim)
        self.use_fp16 = use_fp16
        self._model: Any | None = None

    @property
    def available(self) -> bool:
        """FlagEmbedding 是否已安装（不加载模型）。无副作用。"""
        return importlib.util.find_spec(FLAGEMBEDDING_MODULE) is not None

    def _load(self) -> Any:
        """惰性加载 BGEM3FlagModel；未安装或加载失败抛 EmbedderNotAvailable。

        副作用：首次加载会占内存（BGE-M3 常驻约 2.2 GB）并写一条 info 日志。
        """
        if self._model is not None:
            return self._model
        try:
            from FlagEmbedding import BGEM3FlagModel  # 惰性 import：重依赖（torch），仅需要时安装
        except ImportError as exc:
            raise EmbedderNotAvailable(
                "未安装 FlagEmbedding（pip install 'dramaforge[embed]'），"
                "已降级到 SQL 关键词检索；其余功能不受影响。"
            ) from exc
        logger.info("首次加载 embedding 模型 {}（常驻内存，离线/在线各一次）", self.model_name)
        try:
            self._model = BGEM3FlagModel(self.model_name, use_fp16=self.use_fp16)
        except Exception as exc:  # noqa: BLE001 —— 模型加载失败原因复杂，统一降级
            raise EmbedderNotAvailable(f"加载 embedding 模型 {self.model_name} 失败：{type(exc).__name__}") from exc
        return self._model

    def encode_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """查询 dense 向量。副作用：首次调用加载模型（重）。"""
        return self._encode_dense(list(texts))

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文档 dense 向量（离线批量）。副作用：首次调用加载模型（重）。"""
        return self._encode_dense(list(texts))

    def encode_sparse(self, texts: Sequence[str]) -> list[dict[int, float]]:
        """稀疏词权重 `{token_id: weight}`（只需正权重）。副作用：首次调用加载模型（重）。"""
        model = self._load()
        payloads = _encode(model, list(texts), dense=False, sparse=True)
        out: list[dict[int, float]] = []
        for item in payloads:
            weights = item.get("lexical_weights") or {}
            sparse: dict[int, float] = {}
            for token, weight in weights.items():
                try:
                    token_id = int(token)
                    value = float(weight)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    sparse[token_id] = value
            out.append(sparse)
        return out

    def encode_hybrid(self, texts: Sequence[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
        """一次前向同时取 dense 与 sparse（灌库时省一半推理）。副作用：首次调用加载模型（重）。"""
        model = self._load()
        payloads = _encode(model, list(texts), dense=True, sparse=True)
        dense = [self._check_dim(item.get("dense_vecs") or []) for item in payloads]
        sparse: list[dict[int, float]] = []
        for item in payloads:
            sparse.append(
                {
                    int(token): float(weight)
                    for token, weight in (item.get("lexical_weights") or {}).items()
                    if float(weight) > 0
                }
            )
        return dense, sparse

    def _encode_dense(self, texts: list[str]) -> list[list[float]]:
        """dense 编码 + 维度校验。副作用：首次调用加载模型（重）。"""
        model = self._load()
        payloads = _encode(model, texts, dense=True, sparse=False)
        return [self._check_dim(item.get("dense_vecs") or []) for item in payloads]

    def _check_dim(self, vector: Sequence[float]) -> list[float]:
        """校验向量维度与 EMBED_DIM（collection 维度固定）一致，否则抛 EmbedderNotAvailable。无副作用。"""
        values = [float(v) for v in vector]
        if len(values) != self.dim:
            raise EmbedderNotAvailable(
                f"embedding 维度 {len(values)} 与 EMBED_DIM={self.dim} 不一致，collection 无法写入。"
            )
        return values


def _encode(model: Any, texts: list[str], *, dense: bool, sparse: bool) -> list[dict[str, Any]]:
    """调用 BGE-M3 的 encode；把返回规整成「每文本一项」的 dict 列表。副作用：模型推理。"""
    if not texts:
        return []
    result = model.encode(texts, return_dense=dense, return_sparse=sparse, return_colbert_vecs=False)
    dense_rows = _rows(result.get("dense_vecs"), len(texts))
    lexical_rows = _rows(result.get("lexical_weights"), len(texts))
    return [
        {"dense_vecs": dense_rows[idx] or [], "lexical_weights": lexical_rows[idx] or {}}
        for idx in range(len(texts))
    ]


def _rows(value: Any, count: int) -> list[Any]:
    """把模型返回的批量结果规整成「每文本一行」的列表（兼容单条时的一维数组）。无副作用。"""
    if value is None:
        return [None] * count
    if getattr(value, "ndim", None) == 1:  # 单条文本时部分版本返回一维数组
        return [value] * count
    items = list(value)
    if len(items) == count:
        return items
    if len(items) == 1 and count >= 1:
        return items * count
    return (items + [None] * count)[:count]


@lru_cache(maxsize=4)
def _cached_embedder(model_name: str, dim: int) -> BgeM3Embedder:
    """按 (EMBED_MODEL, EMBED_DIM) 维度缓存 embedder 单例。无副作用。"""
    return BgeM3Embedder(model_name=model_name, dim=dim)


def get_embedder(settings: Settings | None = None) -> BgeM3Embedder:
    """embedder 单例入口。

    AMBIGUITY：Settings 是可变 pydantic 模型、不可哈希，无法直接 `@lru_cache` 到 settings 入参上；
    因此按 (EMBED_MODEL, EMBED_DIM) 缓存（等价单例，且换模型自动换实例）。
    无副作用（返回缓存实例，不加载模型）。
    """
    s = settings or get_settings()
    return _cached_embedder(s.embed_model, int(s.embed_dim))


def clear_embedder_cache() -> None:
    """清空 embedder 缓存（测试或换模型配置时用）。副作用：清缓存。"""
    _cached_embedder.cache_clear()


def _new_client(settings: Settings) -> MilvusClient:
    """按 Settings 建 Milvus 客户端（构造即连接，失败抛 MilvusUnavailable）。副作用：建连接。"""
    try:
        return MilvusClient(uri=settings.milvus_uri, timeout=settings.card_milvus_timeout_sec)
    except Exception as exc:  # noqa: BLE001 —— pymilvus 连接异常类型较杂，统一降级
        raise MilvusUnavailable(f"连接 Milvus 失败（{settings.milvus_uri}）：{type(exc).__name__}") from exc


def _collection_schema(dim: int) -> CollectionSchema:
    """§5.3 的 collection 字段定义：id 主键 + slot_key + text + dense + sparse。无副作用。"""
    fields = [
        FieldSchema(name=ID_FIELD, dtype=DataType.VARCHAR, is_primary=True, max_length=ID_MAX_LEN),
        FieldSchema(name=SLOT_FIELD, dtype=DataType.VARCHAR, max_length=SLOT_MAX_LEN),
        FieldSchema(name=TEXT_FIELD, dtype=DataType.VARCHAR, max_length=TEXT_MAX_LEN),
        FieldSchema(name=DENSE_FIELD, dtype=DataType.FLOAT_VECTOR, dim=int(dim)),
        FieldSchema(name=SPARSE_FIELD, dtype=DataType.SPARSE_FLOAT_VECTOR),
    ]
    return CollectionSchema(fields=fields, description="DramaForge 参照卡镜像（§5.3）")


def ensure_collection(settings: Settings | None = None, drop: bool = False) -> None:
    """建 collection `drama_card` 与两路索引（幂等）。

    dense 用 IP、sparse 用 SPARSE_INVERTED_INDEX；`drop=True` 先删后建。
    副作用：Milvus DDL 与 load；失败抛 MilvusUnavailable（不吞异常）。
    """
    s = settings or get_settings()
    client = _new_client(s)
    try:
        if drop and client.has_collection(s.milvus_collection):
            client.drop_collection(s.milvus_collection)
            logger.warning("dropped milvus collection {}", s.milvus_collection)
        if client.has_collection(s.milvus_collection):
            logger.info("milvus collection {} 已存在，跳过建表", s.milvus_collection)
            return
        index_params = client.prepare_index_params()
        index_params.add_index(field_name=DENSE_FIELD, index_type=DENSE_INDEX_TYPE, metric_type=DENSE_METRIC)
        index_params.add_index(
            field_name=SPARSE_FIELD,
            index_type=SPARSE_INDEX_TYPE,
            metric_type=SPARSE_METRIC,
            params={"inverted_index_algo": SPARSE_INDEX_ALGO},
        )
        client.create_collection(
            collection_name=s.milvus_collection,
            schema=_collection_schema(int(s.embed_dim)),
            index_params=index_params,
            timeout=s.card_milvus_timeout_sec,
        )
        client.load_collection(s.milvus_collection, timeout=s.card_milvus_timeout_sec)
        logger.info("created milvus collection {}（dim={}）", s.milvus_collection, s.embed_dim)
    except Exception as exc:  # noqa: BLE001 —— 统一包装成可降级异常
        raise MilvusUnavailable(f"建 Milvus collection 失败：{type(exc).__name__}：{exc}") from exc
    finally:
        _close_quietly(client)


def _close_quietly(client: Any) -> None:
    """尽力关闭客户端，关闭失败只记 debug。副作用：关闭连接。"""
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 —— 关闭失败不影响主流程
        logger.debug("关闭 Milvus 客户端失败（忽略）")


def rrf_fuse(rankings: list[list[str]], k: int) -> list[tuple[str, float]]:
    """纯函数 RRF 融合：`score(d) = Σ 1/(k + rank)`，rank 从 1 开始。

    - 单路内重复的 id 只计一次（避免刷分），但仍占位名次；跨路重复则分数累加。
    - 同分时按「首次出现顺序」再按 id 字典序，保证结果稳定可复现。
    无副作用。`k < 0` 抛 ValueError（`k + rank` 会 ≤ 0）。
    """
    if k < 0:
        raise ValueError("RRF 的 k 必须 ≥ 0")
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    order = 0
    for ranking in rankings:
        seen_in_route: set[str] = set()
        for rank, doc_id in enumerate(ranking, start=1):
            if doc_id in seen_in_route:
                continue
            seen_in_route.add(doc_id)
            if doc_id not in first_seen:
                first_seen[doc_id] = order
                order += 1
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], first_seen[item[0]], item[0]))


def slot_expr(slot_keys: list[str] | None) -> str | None:
    """slot 过滤表达式；空列表/None 表示不过滤（只接受 SlotKey 字面值）。无副作用。"""
    if not slot_keys:
        return None
    cleaned = [str(key).replace('"', "") for key in dict.fromkeys(slot_keys) if str(key).strip()]
    if not cleaned:
        return None
    joined = ", ".join(f'"{key}"' for key in cleaned)
    return f"{SLOT_FIELD} in [{joined}]"


class MilvusCardRepo:
    """参照卡的 Milvus 仓库。客户端与 embedder 可注入（测试用 Fake），默认按 Settings 惰性创建。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._embedder = embedder

    @property
    def client(self) -> Any:
        """惰性 Milvus 客户端；首次访问才连接（失败抛 MilvusUnavailable）。无副作用（除建连接）。"""
        if self._client is None:
            self._client = _new_client(self.settings)
        return self._client

    @property
    def embedder(self) -> Embedder:
        """embedder 单例（惰性）。无副作用（不加载模型）。"""
        if self._embedder is None:
            self._embedder = get_embedder(self.settings)
        return self._embedder

    def ensure_collection(self, drop: bool = False) -> None:
        """建集合（委托模块级函数，用同一 Settings）。副作用：Milvus DDL。"""
        ensure_collection(self.settings, drop=drop)
        self._client = None

    async def hybrid_search(
        self,
        query: str,
        slot_keys: list[str] | None = None,
        top_k: int | None = None,
    ) -> list[CardHit]:
        """dense + sparse 双 `AnnSearchRequest`，`RRFRanker(k=RRF_K)` 融合后返回 CardHit 列表。

        禁止把两路分数直接相加：融合必须交给 Milvus 的 RRFRanker（§5.3）。
        副作用：一次 Milvus hybrid_search（在工作线程执行，受 CARD_MILVUS_TIMEOUT_SEC 约束）。
        """
        limit = max(1, int(top_k or self.settings.card_top_k))
        dense = self.embedder.encode_queries([query])[0]
        sparse = self.embedder.encode_sparse([query])[0]
        if not sparse:
            logger.warning("query 的 sparse 向量为空，稀疏路将无贡献（不影响 dense 路）")
        expr = slot_expr(slot_keys)
        hits = await self._run(self._hybrid_search_sync, dense, sparse, expr, limit)
        if not hits:
            logger.info("milvus hybrid_search 无命中：query_len={} limit={}", len(query), limit)
        return hits

    async def upsert_cards(self, docs: list[CardDoc]) -> int:
        """离线灌库：按 `id = card_id:slot_key` upsert，缺向量时现算；返回写入条数。

        副作用：embedding 推理 + 一次 Milvus upsert + flush（不吞异常，失败抛 MilvusUnavailable）。
        """
        if not docs:
            return 0
        rows: list[dict[str, Any]] = []
        need_dense = [doc for doc in docs if doc.dense_vector is None]
        need_sparse = [doc for doc in docs if doc.sparse_vector is None]
        dense_map: dict[int, list[float]] = {}
        sparse_map: dict[int, dict[int, float]] = {}
        if need_dense:
            vectors = self.embedder.encode_documents([doc.text for doc in need_dense])
            dense_map = {id(doc): vec for doc, vec in zip(need_dense, vectors)}
        if need_sparse:
            weights = self.embedder.encode_sparse([doc.text for doc in need_sparse])
            sparse_map = {id(doc): vec for doc, vec in zip(need_sparse, weights)}
        for doc in docs:
            dense = doc.dense_vector if doc.dense_vector is not None else dense_map.get(id(doc), [])
            sparse = doc.sparse_vector if doc.sparse_vector is not None else sparse_map.get(id(doc), {})
            rows.append(
                {
                    ID_FIELD: card_milvus_id(doc.card_id, doc.slot_key),
                    SLOT_FIELD: doc.slot_key[:SLOT_MAX_LEN],
                    TEXT_FIELD: doc.text[:TEXT_MAX_LEN],
                    DENSE_FIELD: dense,
                    SPARSE_FIELD: sparse,
                }
            )
        await self._run(self._upsert_sync, rows)
        logger.info("milvus upsert cards: count={} collection={}", len(rows), self.settings.milvus_collection)
        return len(rows)

    # ── 同步内部实现（在工作线程中跑，避免阻塞事件循环）──────
    def _hybrid_search_sync(
        self,
        dense: list[float],
        sparse: dict[int, float],
        expr: str | None,
        limit: int,
    ) -> list[CardHit]:
        """构造两个 AnnSearchRequest 并调用 RRFRanker 融合检索。副作用：一次 Milvus 调用。"""
        dense_req = AnnSearchRequest(
            data=[dense],
            anns_field=DENSE_FIELD,
            param={"metric_type": DENSE_METRIC, "params": {}},
            limit=limit,
            expr=expr,
        )
        sparse_req = AnnSearchRequest(
            data=[sparse],
            anns_field=SPARSE_FIELD,
            param={"metric_type": SPARSE_METRIC, "params": {}},
            limit=limit,
            expr=expr,
        )
        ranker = RRFRanker(k=int(self.settings.rrf_k))
        result = self.client.hybrid_search(
            collection_name=self.settings.milvus_collection,
            reqs=[dense_req, sparse_req],
            ranker=ranker,
            limit=limit,
            output_fields=[SLOT_FIELD, TEXT_FIELD],
            timeout=self.settings.card_milvus_timeout_sec,
        )
        return _hits_from_result(result)

    def _upsert_sync(self, rows: list[dict[str, Any]]) -> None:
        """执行 upsert + flush，让离线灌库立即可检索。副作用：Milvus 写。"""
        self.client.upsert(
            collection_name=self.settings.milvus_collection,
            data=rows,
            timeout=self.settings.card_milvus_timeout_sec,
        )
        self.client.flush(self.settings.milvus_collection, timeout=self.settings.card_milvus_timeout_sec)

    async def _run(self, func: Any, *args: Any) -> Any:
        """在工作线程执行阻塞的 Milvus 调用，并按 CARD_MILVUS_TIMEOUT_SEC 超时；异常包装为 MilvusUnavailable。

        副作用：起线程与等待；超时后线程可能仍在后台收尾（pymilvus 无取消接口）。
        """
        timeout = float(self.settings.card_milvus_timeout_sec)
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
        except MilvusUnavailable:
            raise
        except asyncio.TimeoutError as exc:
            raise MilvusUnavailable(f"Milvus 调用超时（>{timeout}s）") from exc
        except Exception as exc:  # noqa: BLE001 —— 统一降级入口
            raise MilvusUnavailable(f"Milvus 调用失败：{type(exc).__name__}：{exc}") from exc


def _hits_from_result(result: Any) -> list[CardHit]:
    """Milvus 回包（`[[hit, ...]]` 或 `[hit, ...]`）→ CardHit 列表。无副作用。"""
    rows: Any = result
    if isinstance(rows, (list, tuple)) and rows and isinstance(rows[0], (list, tuple)):
        rows = rows[0]
    hits: list[CardHit] = []
    for index, item in enumerate(rows or [], start=1):
        if isinstance(item, dict):
            raw = item
        else:  # 兼容只返回 id 的驱动
            raw = {"id": getattr(item, "id", str(item)), "distance": getattr(item, "distance", 0.0)}
        entity = raw.get("entity") or {}
        milvus_id = str(raw.get("id") or entity.get(ID_FIELD) or "")
        card_id, slot_key = _split_milvus_id(milvus_id)
        slot = str(entity.get(SLOT_FIELD) or raw.get(SLOT_FIELD) or slot_key)
        text = str(entity.get(TEXT_FIELD) or raw.get(TEXT_FIELD) or "")
        score = raw.get("distance", raw.get("score", 0.0))
        hits.append(
            CardHit(
                card_id=card_id,
                slot_key=slot,
                text=text,
                score=_to_float(score),
                rank=index,
                rank_dense=_opt_int(raw.get("rank_dense")),
                rank_sparse=_opt_int(raw.get("rank_sparse")),
            )
        )
    return hits


def _split_milvus_id(milvus_id: str) -> tuple[str, str]:
    """`card_id:slot_key` → (card_id, slot_key)；无冒号时整串当 card_id。无副作用。"""
    if ":" in milvus_id:
        card_id, _, slot_key = milvus_id.rpartition(":")
        return card_id, slot_key
    return milvus_id, ""


def _to_float(value: Any, default: float = 0.0) -> float:
    """任意数值（numpy float / Decimal / str）→ float。无副作用。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_int(value: Any) -> int | None:
    """任意值 → int 或 None（单路名次拿不到时保持 None）。无副作用。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
