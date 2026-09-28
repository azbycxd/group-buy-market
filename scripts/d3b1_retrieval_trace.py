from __future__ import annotations

import argparse
import json

from rag.fusion import reciprocal_rank_fusion
from rag.pipeline import get_rule_search_pipeline


DEFAULT_ORIGINAL = "订单上的待支付、已完成、已关闭分别是什么意思？"
DEFAULT_MODEL_QUERY = "订单状态含义 待支付 已完成 已关闭"


def _items(results: list[object]) -> list[dict[str, object]]:
    return [
        {
            "rank": rank,
            "rule_id": item.rule_id,
            "score": item.score,
        }
        for rank, item in enumerate(results, start=1)
    ]


def _rank(results: list[object], rule_id: str) -> int | None:
    return next(
        (
            rank
            for rank, item in enumerate(results, start=1)
            if item.rule_id == rule_id
        ),
        None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="追踪一条 runtime rule query 的各检索阶段"
    )
    parser.add_argument("--original", default=DEFAULT_ORIGINAL)
    parser.add_argument("--model-query", default=DEFAULT_MODEL_QUERY)
    parser.add_argument("--rule-id", default="ORD-001")
    arguments = parser.parse_args()

    pipeline = get_rule_search_pipeline()
    bm25 = pipeline.bm25.search(arguments.model_query, top_k=20)
    dense = pipeline.dense.search(arguments.model_query, top_k=20)
    first_rrf = pipeline.rrf.search(
        arguments.model_query,
        top_k=len(pipeline.chunks),
    )
    utterance_rrf = pipeline.rrf.search(
        arguments.original,
        top_k=len(pipeline.chunks),
    )
    merged = reciprocal_rank_fusion(
        (first_rrf, utterance_rrf),
        k=60,
        top_k=10,
    )
    reranked = pipeline.reranker.rerank(
        arguments.original,
        merged,
        top_k=10,
    )
    stages = {
        "bm25_top20": bm25,
        "dense_top20": dense,
        "first_rrf": first_rrf,
        "dual_query_merge_top10": merged,
        "rerank_top10": reranked,
    }
    print(
        json.dumps(
            {
                "original": arguments.original,
                "model_query": arguments.model_query,
                "target_rule_id": arguments.rule_id,
                "target_ranks": {
                    name: _rank(results, arguments.rule_id)
                    for name, results in stages.items()
                },
                "stages": {
                    name: _items(results)
                    for name, results in stages.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
