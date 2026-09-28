from __future__ import annotations

from pathlib import Path

import numpy as np

from rag.bm25 import SearchResult
from rag.chunking import RuleChunk
from rag.dense import QUERY_PREFIX, DenseIndex
from rag.fusion import RRFIndex, reciprocal_rank_fusion


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str], **_: object) -> np.ndarray:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            if text.startswith(QUERY_PREFIX):
                vectors.append([1.0, 0.0])
            elif "活动" in text:
                vectors.append([2.0, 0.0])
            else:
                vectors.append([0.0, 3.0])
        return np.asarray(vectors, dtype=np.float32)


def _chunks() -> list[RuleChunk]:
    return [
        RuleChunk("ACT-001", "活动", "活动有效", "活动仍在有效期"),
        RuleChunk("ORD-001", "订单", "订单关闭", "订单已经关闭"),
    ]


def test_dense_uses_query_prefix_normalization_and_content_cache(
    tmp_path: Path,
) -> None:
    first_model = FakeModel()
    first = DenseIndex(
        _chunks(), cache_dir=tmp_path, model=first_model, model_name="fake"
    )
    result = first.search("现在还能参加吗", top_k=2)

    assert result[0].rule_id == "ACT-001"
    assert result[0].score == 1.0
    assert first_model.calls[0] == [chunk.text for chunk in _chunks()]
    assert first_model.calls[1] == [f"{QUERY_PREFIX}现在还能参加吗"]

    second_model = FakeModel()
    DenseIndex(
        _chunks(), cache_dir=tmp_path, model=second_model, model_name="fake"
    )
    assert second_model.calls == []


def _result(rule_id: str) -> SearchResult:
    return SearchResult(rule_id, "section", rule_id, "content", 1.0)


def test_rrf_uses_rank_not_original_score() -> None:
    fused = reciprocal_rank_fusion(
        (
            [_result("A"), _result("B")],
            [_result("B"), _result("A")],
        ),
        k=60,
    )
    assert [item.rule_id for item in fused] == ["A", "B"]
    assert fused[0].score == fused[1].score


class RecordingIndex:
    def __init__(self, chunks: list[RuleChunk], ids: list[str]) -> None:
        self.chunks = chunks
        self.ids = ids
        self.top_ks: list[int] = []

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        self.top_ks.append(top_k)
        return [_result(rule_id) for rule_id in self.ids[:top_k]]


def test_rrf_index_fuses_top_20_from_each_retriever() -> None:
    chunks = _chunks()
    bm25 = RecordingIndex(chunks, ["ACT-001", "ORD-001"])
    dense = RecordingIndex(chunks, ["ORD-001", "ACT-001"])
    index = RRFIndex(bm25, dense)

    index.search("query", top_k=1)

    assert bm25.top_ks == [20]
    assert dense.top_ks == [20]
