from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_middleware import _query_error
from rag.pipeline import RuleSearchHit, RuleSearchResult
from tools.context import AgentContext
from tools.rule_search import search_group_buy_rules


class FakePipeline:
    def __init__(self, matches: tuple[RuleSearchHit, ...]) -> None:
        self.matches = matches
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> RuleSearchResult:
        self.calls.append(kwargs)
        return RuleSearchResult(
            matches=self.matches,
            top_score=self.matches[0].rerank_score if self.matches else 0.1,
            candidate_rule_ids=("ACT-001",),
        )


class RuleSearchTests(unittest.TestCase):
    def test_runtime_context_is_not_a_model_argument(self) -> None:
        schema = search_group_buy_rules.tool_call_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"query"})

    def test_trusted_user_text_drives_rerank_and_top3_is_returned(self) -> None:
        matches = tuple(
            RuleSearchHit(
                rule_id=f"RULE-{index}",
                title=f"规则 {index}",
                content=f"内容 {index}",
                rerank_score=0.99 - index / 10,
            )
            for index in range(3)
        )
        pipeline = FakePipeline(matches)
        runtime = SimpleNamespace(
            context=AgentContext(
                user_id="demo-user",
                user_text="用户未经改写的原话",
            )
        )
        with (
            patch(
                "tools.rule_search.get_rule_search_pipeline",
                return_value=pipeline,
            ),
            patch.dict(os.environ, {"RAG_RERANK_THRESHOLD": "0.85"}),
        ):
            result = search_group_buy_rules.func(
                query="模型生成的检索词",
                runtime=runtime,
            )

        self.assertEqual(
            pipeline.calls,
            [
                {
                    "model_query": "模型生成的检索词",
                    "original_utterance": "用户未经改写的原话",
                    "threshold": 0.85,
                }
            ],
        )
        returned = result["data"]["matches"]
        self.assertEqual(len(returned), 3)
        self.assertEqual(
            set(returned[0]),
            {"rule_id", "title", "content", "rerank_score"},
        )
        self.assertNotIn("source", str(result))

    def test_rejected_search_returns_empty_matches(self) -> None:
        pipeline = FakePipeline(())
        with patch(
            "tools.rule_search.get_rule_search_pipeline",
            return_value=pipeline,
        ):
            result = search_group_buy_rules.func(
                query="包邮",
                runtime=SimpleNamespace(
                    context=AgentContext(
                        user_id="demo-user",
                        user_text="拼团商品包邮吗？",
                    )
                ),
            )
        self.assertEqual(result["data"]["matches"], [])

    def test_semantic_query_rewrite_is_allowed_before_trusted_rerank(self) -> None:
        request = SimpleNamespace(
            runtime=SimpleNamespace(
                context=AgentContext(
                    user_id="demo-user",
                    user_text="钱付了但人没凑齐，能退吗？",
                )
            )
        )
        self.assertIsNone(
            _query_error("已支付未成团退款规则", request)
        )
        self.assertIsNotNone(_query_error("", request))


if __name__ == "__main__":
    unittest.main()
