from __future__ import annotations

from time import perf_counter
from typing import Any, Protocol, Sequence

import numpy as np

from rag.bm25 import SearchResult
from rag.chunking import RuleChunk


MODEL_NAME = "BAAI/bge-reranker-base"
CANDIDATE_COUNT = 10


class SearchIndex(Protocol):
    chunks: list[RuleChunk]

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]: ...


class RerankIndex:
    def __init__(
        self,
        candidate_index: SearchIndex,
        *,
        model_name: str = MODEL_NAME,
        candidate_count: int = CANDIDATE_COUNT,
        batch_size: int = CANDIDATE_COUNT,
        device: str = "cpu",
        model: Any | None = None,
    ) -> None:
        if candidate_count < 1:
            raise ValueError("Rerank candidate_count 必须大于 0")
        if batch_size < 1:
            raise ValueError("Rerank batch_size 必须大于 0")
        self.candidate_index = candidate_index
        self.chunks = candidate_index.chunks
        self.model_name = model_name
        self.candidate_count = candidate_count
        self.batch_size = batch_size
        self.device = device
        self.latencies_ms: list[float] = []
        self._model = model or self._load_model(model_name, device)

    @staticmethod
    def _load_model(model_name: str, device: str) -> Any:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as error:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Rerank 需要 sentence-transformers"
            ) from error
        return CrossEncoder(model_name, device=device)

    def rerank(
        self,
        query: str,
        candidates: Sequence[SearchResult],
        *,
        top_k: int,
    ) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        if not candidates:
            return []

        started = perf_counter()
        pairs = [
            (query, f"{candidate.title} {candidate.content}")
            for candidate in candidates
        ]
        predicted = self._model.predict(
            pairs,
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            device=self.device,
        )
        scores = np.asarray(predicted, dtype=np.float32).reshape(-1)
        if len(scores) != len(candidates) or not np.all(np.isfinite(scores)):
            raise ValueError("CrossEncoder 返回的分数数量或数值非法")
        ranked = sorted(
            zip(scores.tolist(), candidates, strict=True),
            key=lambda item: (-item[0], item[1].rule_id),
        )
        results = [
            SearchResult(
                rule_id=candidate.rule_id,
                section=candidate.section,
                title=candidate.title,
                content=candidate.content,
                score=float(score),
            )
            for score, candidate in ranked[: min(top_k, len(ranked))]
        ]
        self.latencies_ms.append((perf_counter() - started) * 1000)
        return results

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        candidates = self.candidate_index.search(
            query, top_k=self.candidate_count
        )
        return self.rerank(query, candidates, top_k=top_k)
