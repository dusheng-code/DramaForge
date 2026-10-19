"""职责：M1 验收单测——RRF 融合公式/顺序/稳定性/k 影响/空输入，以及 hybrid_search 是否真的走「双 AnnSearchRequest + RRFRanker」。
输入：纯函数入参、Fake 版的 pymilvus 符号与 Milvus 客户端（类名带 Fake 前缀，见 §0.3）。
输出：pytest 断言结果；不产生任何真实 Milvus / MySQL / 网络副作用。

副作用：仅在进程内构造假对象与假向量；不连 Milvus、不写磁盘、不写库。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dramaforge.core.config import Settings
from dramaforge.retrieval import milvus_repo
from dramaforge.retrieval.milvus_repo import (
    DENSE_FIELD,
    SPARSE_FIELD,
    CardHit,
    MilvusCardRepo,
    MilvusUnavailable,
    rrf_fuse,
)

K = 60


# ── Fake 依赖（§0.3：Fake 只在 tests/，类名带 Fake 前缀）────────────
class FakeAnnSearchRequest:
    """记录 AnnSearchRequest 的构造参数，验证「两路各一次」。"""

    instances: list["FakeAnnSearchRequest"] = []

    def __init__(
        self,
        data: Any,
        anns_field: str,
        param: dict[str, Any],
        limit: int,
        expr: str | None = None,
        **_: Any,
    ) -> None:
        self.data = data
        self.anns_field = anns_field
        self.param = param
        self.limit = limit
        self.expr = expr
        FakeAnnSearchRequest.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        """清空历史记录。副作用：类级列表被清空。"""
        cls.instances = []


class FakeRRFRanker:
    """记录 RRF 的 k，验证融合走的是 RRFRanker 而不是分数相加。"""

    instances: list["FakeRRFRanker"] = []

    def __init__(self, k: int = 60) -> None:
        self.k = k
        FakeRRFRanker.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        """清空历史记录。副作用：类级列表被清空。"""
        cls.instances = []


class FakeEmbedder:
    """固定维度的假 embedder：不加载 FlagEmbedding，直接给常量向量。"""

    model_name = "FAKE-BGE"
    dim = 4

    @property
    def available(self) -> bool:
        """永远可用。无副作用。"""
        return True

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        """返回常量 dense 向量。无副作用。"""
        return [[0.25, 0.25, 0.25, 0.25] for _ in texts]

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        """返回常量 dense 向量。无副作用。"""
        return [[0.5, 0.5, 0.0, 0.0] for _ in texts]

    def encode_sparse(self, texts: list[str]) -> list[dict[int, float]]:
        """返回常量稀疏权重。无副作用。"""
        return [{11: 0.7, 22: 0.3} for _ in texts]


class FakeMilvusClient:
    """假 Milvus 客户端：记录 hybrid_search 的入参并回放预置命中。"""

    def __init__(self, hits: list[dict[str, Any]] | None = None, delay_sec: float = 0.0) -> None:
        self.hits = hits or []
        self.delay_sec = delay_sec
        self.calls: list[dict[str, Any]] = []

    def hybrid_search(
        self,
        collection_name: str,
        reqs: list[Any],
        ranker: Any,
        limit: int = 10,
        output_fields: list[str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> list[list[dict[str, Any]]]:
        """记录一次融合检索并返回预置命中（形如 Milvus 的 `[[hit, ...]]`）。副作用：内存记录。"""
        if self.delay_sec:
            import time

            time.sleep(self.delay_sec)
        self.calls.append(
            {
                "collection_name": collection_name,
                "reqs": list(reqs),
                "ranker": ranker,
                "limit": limit,
                "output_fields": output_fields,
                "timeout": timeout,
            }
        )
        return [self.hits[:limit]]


def _hit(milvus_id: str, slot: str, text: str, distance: float) -> dict[str, Any]:
    """构造一条 Milvus 命中行。无副作用。"""
    return {"id": milvus_id, "distance": distance, "entity": {"slot_key": slot, "text": text}}


# ── 1. RRF 公式正确性（手算对照）────────────────────────────
def test_rrf_formula_matches_hand_computation() -> None:
    """单路 + 双路叠加的分数必须等于 Σ 1/(k+rank) 的手算值。无副作用。"""
    rankings = [["a", "b", "c"], ["c", "d"]]

    fused = dict(rrf_fuse(rankings, K))

    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert fused["d"] == pytest.approx(1 / 62)


def test_rrf_rank_starts_at_one_not_zero() -> None:
    """rank 从 1 开始：单条命中的分数必须是 1/(k+1) 而不是 1/k。无副作用。"""
    fused = rrf_fuse([["only"]], K)

    assert fused == [("only", pytest.approx(1 / (K + 1)))]


# ── 2. 两路互补时的融合顺序 ────────────────────────────────
def test_rrf_complementary_rankings_orders_shared_doc_first() -> None:
    """dense 与 sparse 各贡献一部分时，两路都出现的文档必须排第一。无副作用。"""
    dense_ranking = ["x", "y"]
    sparse_ranking = ["y", "z"]

    fused = rrf_fuse([dense_ranking, sparse_ranking], K)

    assert [doc for doc, _ in fused] == ["y", "x", "z"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_rrf_disjoint_rankings_keep_route_order() -> None:
    """两路完全不相交且同分时，按首次出现顺序（先 dense 后 sparse）稳定输出。无副作用。"""
    fused = rrf_fuse([["d1", "d2"], ["s1", "s2"]], K)

    assert [doc for doc, _ in fused] == ["d1", "s1", "d2", "s2"]


# ── 3. 同分稳定性 ─────────────────────────────────────────
def test_rrf_ties_are_stable_and_deterministic() -> None:
    """完全对称的输入会产出同分，多次调用顺序必须完全一致（按首次出现顺序）。无副作用。"""
    rankings = [["a", "b"], ["b", "a"]]

    runs = [rrf_fuse(rankings, K) for _ in range(5)]

    assert all(run == runs[0] for run in runs)
    assert [doc for doc, _ in runs[0]] == ["a", "b"]
    assert runs[0][0][1] == pytest.approx(runs[0][1][1])


def test_rrf_tie_break_follows_first_appearance() -> None:
    """同分时，先出现的文档排在前面（与输入路顺序一致，可复现）。无副作用。"""
    assert [doc for doc, _ in rrf_fuse([["b"], ["a"]], K)] == ["b", "a"]
    assert [doc for doc, _ in rrf_fuse([["a"], ["b"]], K)] == ["a", "b"]


# ── 4. k 值影响 ───────────────────────────────────────────
def test_rrf_k_dampens_gap_between_adjacent_ranks() -> None:
    """k 越大，相邻名次的分数差越小（平滑作用），但名次顺序不变。无副作用。"""
    small_k = dict(rrf_fuse([["a", "b"]], 1))
    middle_k = dict(rrf_fuse([["a", "b"]], 60))
    huge_k = dict(rrf_fuse([["a", "b"]], 1000))

    assert small_k["a"] > small_k["b"] > 0
    assert small_k["a"] - small_k["b"] > middle_k["a"] - middle_k["b"] > huge_k["a"] - huge_k["b"]
    assert middle_k["a"] == pytest.approx(1 / 61)
    assert huge_k["a"] == pytest.approx(1 / 1001)


def test_rrf_k_zero_uses_plain_reciprocal_rank() -> None:
    """k=0 时退化为 1/rank，可用于对照公式。无副作用。"""
    fused = dict(rrf_fuse([["a", "b", "c"]], 0))

    assert fused == {"a": pytest.approx(1.0), "b": pytest.approx(0.5), "c": pytest.approx(1 / 3)}


def test_rrf_rejects_negative_k() -> None:
    """k<0 会让 k+rank ≤ 0 除以零，必须显式拒绝。无副作用。"""
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], -1)


# ── 5. 空输入 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "rankings",
    [
        [],
        [[]],
        [[], []],
    ],
)
def test_rrf_empty_inputs_return_empty(rankings: list[list[str]]) -> None:
    """无任何排名时返回空列表（不抛异常）。无副作用。"""
    assert rrf_fuse(rankings, K) == []


def test_rrf_duplicate_id_in_one_route_counts_once() -> None:
    """同一路里重复的文档只计一次分（仍占名次位），跨路重复才累加。无副作用。"""
    fused = dict(rrf_fuse([["a", "a", "b"]], K))

    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 63)  # 名次按物理位置，重复项不压掉后续名次


# ── 6. hybrid_search 真的走「双 AnnSearchRequest + RRFRanker」────
@pytest.fixture()
def fake_milvus(monkeypatch: pytest.MonkeyPatch):
    """把 milvus_repo 里的 AnnSearchRequest / RRFRanker 换成 Fake，并清空历史。无外部副作用。"""
    FakeAnnSearchRequest.reset()
    FakeRRFRanker.reset()
    monkeypatch.setattr(milvus_repo, "AnnSearchRequest", FakeAnnSearchRequest)
    monkeypatch.setattr(milvus_repo, "RRFRanker", FakeRRFRanker)
    return FakeAnnSearchRequest, FakeRRFRanker


async def test_hybrid_search_issues_two_ann_requests_and_rrf_ranker(
    settings: Settings,
    fake_milvus: tuple[type[FakeAnnSearchRequest], type[FakeRRFRanker]],
) -> None:
    """hybrid_search 必须发两次 AnnSearchRequest（dense/sparse）并用 RRFRanker(k=RRF_K) 融合。

    同时断言两路分数**没有被 Python 相加**：返回分数就是 Milvus 融合后的 distance。
    无外部副作用：Milvus 与 embedder 均为 Fake。
    """
    request_cls, ranker_cls = fake_milvus
    hits = [_hit("c1:premise", "premise", "前三秒必须有冲突", 0.032), _hit("c2:risk", "risk", "禁用真实品牌", 0.016)]
    client = FakeMilvusClient(hits=hits)
    repo = MilvusCardRepo(settings, client=client, embedder=FakeEmbedder())

    result = await repo.hybrid_search("前三秒冲突", None, top_k=2)

    # 两次 AnnSearchRequest，分别针对 dense 与 sparse 字段
    assert len(request_cls.instances) == 2
    assert [req.anns_field for req in request_cls.instances] == [DENSE_FIELD, SPARSE_FIELD]
    assert request_cls.instances[0].limit == 2
    assert request_cls.instances[1].limit == 2
    assert request_cls.instances[0].expr is None

    # 融合必须交给 RRFRanker，k 取自 settings.rrf_k
    assert len(ranker_cls.instances) == 1
    assert ranker_cls.instances[0].k == settings.rrf_k

    # 只调一次 hybrid_search，且两个请求一起传入
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["collection_name"] == settings.milvus_collection
    assert len(call["reqs"]) == 2
    assert isinstance(call["ranker"], FakeRRFRanker)
    assert call["limit"] == 2
    assert call["timeout"] == settings.card_milvus_timeout_sec

    # 命中的映射与分数透传（不做手动相加）
    assert [hit.card_id for hit in result] == ["c1", "c2"]
    assert [hit.slot_key for hit in result] == ["premise", "risk"]
    assert [hit.rank for hit in result] == [1, 2]
    assert result[0].score == pytest.approx(0.032)
    assert result[0].text == "前三秒必须有冲突"
    assert result[0].source == "milvus"
    assert isinstance(result[0], CardHit)


async def test_hybrid_search_passes_slot_expr_when_filtered(
    settings: Settings,
    fake_milvus: tuple[type[FakeAnnSearchRequest], type[FakeRRFRanker]],
) -> None:
    """给了 slot_keys 时，两路请求都要带同一过滤表达式（而不是在 Python 里后过滤）。无副作用。"""
    request_cls, _ = fake_milvus
    repo = MilvusCardRepo(settings, client=FakeMilvusClient(), embedder=FakeEmbedder())

    await repo.hybrid_search("冲突", ["premise", "risk"], top_k=4)

    assert [req.expr for req in request_cls.instances] == [
        "slot_key in [\"premise\", \"risk\"]",
        "slot_key in [\"premise\", \"risk\"]",
    ]


async def test_hybrid_search_wraps_failures_as_milvus_unavailable(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """底层 Milvus 抛异常时必须包装成 MilvusUnavailable（供 cards.py 降级），不能静默成功。无副作用。"""

    class FakeBrokenClient:
        """任何调用都抛错的假客户端。"""

        def hybrid_search(self, **_: Any) -> Any:
            """模拟 gRPC 连接被拒。副作用：抛异常。"""
            raise ConnectionError("grpc unreachable")

    repo = MilvusCardRepo(settings, client=FakeBrokenClient(), embedder=FakeEmbedder())

    with pytest.raises(MilvusUnavailable):
        await repo.hybrid_search("冲突", None, top_k=2)


async def test_hybrid_search_times_out_per_settings(fake_milvus: tuple[type[FakeAnnSearchRequest], type[FakeRRFRanker]]) -> None:
    """CARD_MILVUS_TIMEOUT_SEC 到点必须抛 MilvusUnavailable（不无限等待）。副作用：等待 ~0.05s。"""
    settings = Settings().model_copy(update={"card_milvus_timeout_sec": 0.05})
    repo = MilvusCardRepo(settings, client=FakeMilvusClient(delay_sec=0.5), embedder=FakeEmbedder())

    with pytest.raises(MilvusUnavailable):
        await repo.hybrid_search("冲突", None, top_k=2)
    await asyncio.sleep(0)  # 让后台线程的收尾不影响后续用例


def test_get_embedder_is_cached_singleton() -> None:
    """embedder 单例按 (EMBED_MODEL, EMBED_DIM) 缓存：同配置两次调用返回同一对象。无副作用。"""
    settings = Settings()
    first = milvus_repo.get_embedder(settings)
    second = milvus_repo.get_embedder(settings)

    assert first is second
