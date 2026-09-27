from __future__ import annotations

import unittest
from types import SimpleNamespace

from tools.context import AgentContext
from tools.rule_search import search_group_buy_rules


def _search(user_text: str, query: str) -> list[dict[str, object]]:
    result = search_group_buy_rules.func(
        query=query,
        runtime=SimpleNamespace(
            context=AgentContext(user_id="demo-user", user_text=user_text)
        ),
    )
    return result["data"]["matches"]


class RuleSearchTests(unittest.TestCase):
    def test_runtime_context_is_not_a_model_argument(self) -> None:
        schema = search_group_buy_rules.tool_call_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"query"})

    def test_activity_keyword_in_user_text_allows_activity_rule(self) -> None:
        matches = _search(
            "拼团活动有效期方面有什么规则？",
            "活动有效期和可加入团队规则",
        )
        self.assertEqual(
            [item["rule_id"] for item in matches],
            ["activity_validity"],
        )

    def test_model_query_cannot_manufacture_team_match(self) -> None:
        for user_text in (
            "优惠能否与会员券叠加？",
            "拼团商品是否包邮？",
            "拼团购买后能开增值税发票吗？",
        ):
            with self.subTest(user_text=user_text):
                self.assertEqual(
                    _search(user_text, "拼团团队加入和队伍规则"),
                    [],
                )

    def test_distinct_team_topic_still_matches(self) -> None:
        matches = _search(
            "拼团队伍达到人数上限时有什么规则？",
            "队伍人数上限规则",
        )
        self.assertEqual(
            [item["rule_id"] for item in matches],
            ["joinable_team"],
        )


if __name__ == "__main__":
    unittest.main()
