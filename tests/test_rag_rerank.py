from __future__ import annotations

from typing import Sequence

import numpy as np

from rag.bm25 import SearchResult
from rag.chunking import RuleChunk
from rag.rerank import RerankIndex
from scripts.rag_eval import select_threshold


def _result(rule_id: str, score: float = 0.0) -> SearchResult:
    return SearchResult(
        rule_id=rule_id,
        section="section",
        title=f"title-{rule_id}",
        content=f"content-{rule_id}",
        score=score,
    )


class CandidateIndex:
    def __init__(self, count: int = 12) -> None:
        self.chunks = [
            RuleChunk(f"R-{index:03d}", "section", "title", "content")
            for index in range(count)
        ]
        self.requested: list[int] = []

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        self.requested.append(top_k)
        return [_result(chunk.rule_id) for chunk in self.chunks[:top_k]]


class FakeCrossEncoder:
    def __init__(self, scores: Sequence[float]) -> None:
        self.scores = np.asarray(scores, dtype=np.float32)
        self.pairs: list[tuple[str, str]] = []

    def predict(
        self, pairs: list[tuple[str, str]], **_: object
    ) -> np.ndarray:
        self.pairs = pairs
        return self.scores[: len(pairs)]


def test_rerank_uses_rrf_top10_and_title_content_only() -> None:
    candidates = CandidateIndex()
    model = FakeCrossEncoder([float(index) for index in range(10)])
    reranker = RerankIndex(candidates, model=model)

    results = reranker.search("用户问题", top_k=3)

    assert candidates.requested == [10]
    assert [result.rule_id for result in results] == ["R-009", "R-008", "R-007"]
    assert model.pairs[0] == (
        "用户问题",
        "title-R-000 content-R-000",
    )
    assert len(model.pairs) == 10
    assert len(reranker.latencies_ms) == 1
    assert reranker.latencies_ms[0] >= 0


def test_threshold_tie_prefers_fewer_false_answers() -> None:
    selected = select_threshold(
        answerable_scores=[0.4, 0.9],
        unanswerable_scores=[0.3, 0.8],
    )

    assert selected.threshold == 0.9
    assert selected.false_reject == 1
    assert selected.false_answer == 0
    assert selected.balanced_accuracy == 0.75
