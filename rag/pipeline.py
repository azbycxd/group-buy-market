from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass

from rag.bm25 import BM25Index, SearchResult
from rag.chunking import RuleChunk, load_rule_chunks
from rag.dense import DenseIndex
from rag.fusion import RRFIndex, reciprocal_rank_fusion
from rag.rerank import CANDIDATE_COUNT, RerankIndex


# Exploratory threshold selected on the small D2.1 evaluation set.
DEFAULT_RERANK_THRESHOLD = 0.85
FINAL_RERANK_COUNT = 3
FINAL_RRF_COUNT = 3


@dataclass(frozen=True, slots=True)
class RuleSearchHit:
    rule_id: str
    title: str
    content: str
    rerank_score: float


@dataclass(frozen=True, slots=True)
class RuleSearchResult:
    matches: tuple[RuleSearchHit, ...]
    top_score: float | None
    candidate_rule_ids: tuple[str, ...]


class RuleSearchPipeline:
    def __init__(self) -> None:
        self.chunks: list[RuleChunk] = load_rule_chunks()
        self.bm25 = BM25Index(self.chunks)
        self.dense = DenseIndex(self.chunks)
        self.rrf = RRFIndex(
            self.bm25,
            self.dense,
            k=60,
            candidate_count=20,
        )
        self.reranker = RerankIndex(
            self.rrf,
            candidate_count=CANDIDATE_COUNT,
            batch_size=CANDIDATE_COUNT,
            device="cpu",
        )

    def _rrf_candidates(self, query: str) -> list[SearchResult]:
        return self.rrf.search(query, top_k=len(self.chunks))

    def search(
        self,
        *,
        model_query: str,
        original_utterance: str,
        threshold: float,
    ) -> RuleSearchResult:
        model_ranking = self._rrf_candidates(model_query)
        utterance_ranking = self._rrf_candidates(original_utterance)
        merged = reciprocal_rank_fusion(
            (model_ranking, utterance_ranking),
            k=60,
            top_k=CANDIDATE_COUNT,
        )
        reranked = self.reranker.rerank(
            original_utterance,
            merged,
            top_k=CANDIDATE_COUNT,
        )
        top_score = reranked[0].score if reranked else None
        matches: tuple[RuleSearchHit, ...] = ()
        if top_score is not None and top_score >= threshold:
            reranked_by_id = {item.rule_id: item for item in reranked}
            final_candidates: list[SearchResult] = []
            seen_rule_ids: set[str] = set()
            for item in (
                *reranked[:FINAL_RERANK_COUNT],
                *merged[:FINAL_RRF_COUNT],
            ):
                if item.rule_id in seen_rule_ids:
                    continue
                # Every merged Top3 item was scored in the full rerank Top10;
                # expose its CrossEncoder score, not its RRF fusion score.
                scored = reranked_by_id[item.rule_id]
                final_candidates.append(scored)
                seen_rule_ids.add(item.rule_id)
            matches = tuple(
                RuleSearchHit(
                    rule_id=item.rule_id,
                    title=item.title,
                    content=item.content,
                    rerank_score=item.score,
                )
                for item in final_candidates
            )
        return RuleSearchResult(
            matches=matches,
            top_score=top_score,
            candidate_rule_ids=tuple(item.rule_id for item in merged),
        )


def rerank_threshold() -> float:
    raw = os.getenv(
        "RAG_RERANK_THRESHOLD",
        str(DEFAULT_RERANK_THRESHOLD),
    )
    try:
        value = float(raw)
    except ValueError as error:
        raise RuntimeError("RAG_RERANK_THRESHOLD 必须是数字") from error
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise RuntimeError("RAG_RERANK_THRESHOLD 必须在 [0, 1] 范围内")
    return value


_PIPELINE: RuleSearchPipeline | None = None
_PIPELINE_LOCK = threading.Lock()


def get_rule_search_pipeline() -> RuleSearchPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        with _PIPELINE_LOCK:
            if _PIPELINE is None:
                _PIPELINE = RuleSearchPipeline()
    return _PIPELINE


def warmup_rule_search_pipeline() -> RuleSearchPipeline:
    """Load all public-rule retrieval assets before HTTP readiness."""
    return get_rule_search_pipeline()
