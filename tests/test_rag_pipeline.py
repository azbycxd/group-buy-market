from __future__ import annotations

from rag.bm25 import SearchResult
from rag.chunking import RuleChunk
import rag.pipeline as pipeline_module
from rag.pipeline import RuleSearchPipeline


def _result(rule_id: str, score: float = 0.0) -> SearchResult:
    return SearchResult(
        rule_id=rule_id,
        section="section",
        title=f"title-{rule_id}",
        content=f"content-{rule_id}",
        score=score,
    )


class FakeRRF:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        self.calls.append((query, top_k))
        prefix = "M" if query == "模型改写" else "U"
        shared = [_result("SHARED")]
        unique = [_result(f"{prefix}-{index:02d}") for index in range(11)]
        return (shared + unique)[:top_k]


class FakeReranker:
    def __init__(self, top_score: float = 0.9) -> None:
        self.top_score = top_score
        self.query = ""
        self.candidate_ids: list[str] = []

    def rerank(
        self,
        query: str,
        candidates: list[SearchResult],
        *,
        top_k: int,
    ) -> list[SearchResult]:
        self.query = query
        self.candidate_ids = [item.rule_id for item in candidates]
        scores = [self.top_score - 0.01 * index for index in range(top_k)]
        reranked = list(reversed(candidates[:top_k]))
        return [
            _result(candidate.rule_id, score)
            for candidate, score in zip(
                reranked, scores, strict=True
            )
        ]


def _pipeline(top_score: float = 0.9) -> RuleSearchPipeline:
    pipeline = object.__new__(RuleSearchPipeline)
    pipeline.chunks = [
        RuleChunk(f"R-{index:02d}", "s", "t", "c")
        for index in range(12)
    ]
    pipeline.rrf = FakeRRF()
    pipeline.reranker = FakeReranker(top_score)
    return pipeline


def test_dual_query_merge_and_original_utterance_rerank() -> None:
    pipeline = _pipeline()
    result = pipeline.search(
        model_query="模型改写",
        original_utterance="用户原话",
        threshold=0.85,
    )

    assert pipeline.rrf.calls == [
        ("模型改写", 12),
        ("用户原话", 12),
    ]
    assert pipeline.reranker.query == "用户原话"
    assert len(pipeline.reranker.candidate_ids) == 10
    assert len(result.matches) == 6
    assert [item.rule_id for item in result.matches[:3]] == list(
        reversed(pipeline.reranker.candidate_ids)
    )[:3]
    assert [item.rule_id for item in result.matches[3:]] == (
        pipeline.reranker.candidate_ids[:3]
    )
    assert result.matches[0].rerank_score == 0.9
    assert result.matches[3].rerank_score < result.matches[2].rerank_score


def test_rerank_score_below_threshold_returns_no_matches() -> None:
    result = _pipeline(top_score=0.849).search(
        model_query="模型改写",
        original_utterance="用户原话",
        threshold=0.85,
    )

    assert result.matches == ()
    assert result.top_score == 0.849


def test_pipeline_is_lazily_created_once(monkeypatch: object) -> None:
    created: list[object] = []

    class FakePipeline:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr(pipeline_module, "_PIPELINE", None)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        pipeline_module,
        "RuleSearchPipeline",
        FakePipeline,
    )

    first = pipeline_module.get_rule_search_pipeline()
    second = pipeline_module.get_rule_search_pipeline()

    assert first is second
    assert created == [first]
