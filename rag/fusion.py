from __future__ import annotations

from collections import defaultdict
from typing import Protocol, Sequence

from rag.bm25 import SearchResult
from rag.chunking import RuleChunk


class SearchIndex(Protocol):
    chunks: list[RuleChunk]

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]: ...


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchResult]],
    *,
    k: int = 60,
    top_k: int | None = None,
) -> list[SearchResult]:
    if k < 1:
        raise ValueError("RRF k 必须大于 0")
    if top_k is not None and top_k < 1:
        raise ValueError("top_k 必须大于 0")

    scores: dict[str, float] = defaultdict(float)
    results_by_id: dict[str, SearchResult] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, result in enumerate(ranking, start=1):
            if result.rule_id in seen:
                continue
            seen.add(result.rule_id)
            results_by_id.setdefault(result.rule_id, result)
            scores[result.rule_id] += 1.0 / (k + rank)

    fused = sorted(scores, key=lambda rule_id: (-scores[rule_id], rule_id))
    if top_k is not None:
        fused = fused[:top_k]
    return [
        SearchResult(
            rule_id=rule_id,
            section=results_by_id[rule_id].section,
            title=results_by_id[rule_id].title,
            content=results_by_id[rule_id].content,
            score=scores[rule_id],
        )
        for rule_id in fused
    ]


class RRFIndex:
    def __init__(
        self,
        bm25: SearchIndex,
        dense: SearchIndex,
        *,
        k: int = 60,
        candidate_count: int = 20,
    ) -> None:
        if k < 1:
            raise ValueError("RRF k 必须大于 0")
        if candidate_count < 1:
            raise ValueError("candidate_count 必须大于 0")
        self.bm25 = bm25
        self.dense = dense
        self.k = k
        self.candidate_count = candidate_count
        self.chunks = bm25.chunks

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        bm25_results = self.bm25.search(
            query, top_k=self.candidate_count
        )
        dense_results = self.dense.search(
            query, top_k=self.candidate_count
        )
        return reciprocal_rank_fusion(
            (bm25_results, dense_results),
            k=self.k,
            top_k=top_k,
        )
