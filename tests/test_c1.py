from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from action_ledger import AgentActionStore
from outcome import OutcomeKind
from tests.test_b2 import free_port, stop_process


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.getenv("RUN_C1_INTEGRATION") == "1"


def _tool_names(result: dict[str, object]) -> list[str]:
    return [
        call["name"]
        for message in result.get("messages", [])
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]


@unittest.skipUnless(
    RUN_INTEGRATION,
    "设置 RUN_C1_INTEGRATION=1 后运行 C1 集成测试",
)
class C1RefundProposalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from evals.run import start_fake_java

        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-c1-",
            dir=PROJECT_ROOT,
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.action_path = cls.temp_path / "agent_actions.sqlite"
        os.environ["AGENT_ACTION_DB_PATH"] = str(cls.action_path)
        cls.fake_port = free_port()
        cls.fake_process = start_fake_java(cls.fake_port)
        os.environ["FAKE_JAVA_BASE_URL"] = (
            f"http://127.0.0.1:{cls.fake_port}"
        )

        from agent import create_order_agent

        cls.agent = create_order_agent(cls.temp_path / "checkpoints.sqlite")

    @classmethod
    def tearDownClass(cls) -> None:
        from agent import close_order_agent

        close_order_agent(cls.agent)
        stop_process(cls.fake_process)
        os.environ.pop("AGENT_ACTION_DB_PATH", None)
        cls.temporary_directory.cleanup()

    def invoke(self, text: str) -> dict[str, object]:
        return self.agent.invoke(
            {"messages": [HumanMessage(content=text)]},
            context=self.context_type(user_id="demo-user"),
            config={
                "configurable": {
                    "thread_id": f"c1-{uuid.uuid4().hex}",
                }
            },
        )

    @property
    def context_type(self):
        from tools.context import AgentContext

        return AgentContext

    def actions(self) -> list[dict[str, object]]:
        return AgentActionStore(self.action_path).list_actions()

    def test_1_refund_policy_searches_rules_without_action(self) -> None:
        before = len(self.actions())
        for _ in range(3):
            result = self.invoke("退款条件是什么？")
            self.assertEqual(
                [need.value for need in result["understanding"].needs],
                ["REFUND_POLICY"],
            )
            self.assertIn("search_group_buy_rules", _tool_names(result))
        self.assertEqual(len(self.actions()), before)

    def test_2_refundable_order_creates_one_proposal_per_case(self) -> None:
        before = len(self.actions())
        for _ in range(3):
            result = self.invoke("帮我把订单 ORD200001 退了")
            self.assertEqual(result["outcome"].kind, OutcomeKind.ANSWER)
            self.assertIn("action_id", result["outcome"].final_answer)
            self.assertIn("没有执行任何退款", result["outcome"].final_answer)
        created = self.actions()[before:]
        self.assertEqual(len(created), 3)
        for action in created:
            self.assertEqual(action["action_type"], "REFUND")
            self.assertEqual(action["status"], "PROPOSED")
            self.assertEqual(action["out_trade_no"], "ORD200001")
            self.assertEqual(
                action["expected_version"],
                (
                    "2026-09-27T10:00:00+08:00|"
                    "2026-09-27T10:05:00+08:00"
                ),
            )
            preview = json.loads(action["preview_json"])
            self.assertEqual(
                preview,
                {
                    "orderStatus": "COMPLETE",
                    "teamStatus": "PROGRESS",
                    "refundType": "PAID_UNFORMED",
                    "refundProposalAllowed": True,
                    "requiresManualReview": False,
                    "orderUpdateTime": "2026-09-27T10:00:00+08:00",
                    "teamUpdateTime": "2026-09-27T10:05:00+08:00",
                },
            )

    def test_3_closed_order_does_not_create_proposal(self) -> None:
        before = len(self.actions())
        for _ in range(3):
            result = self.invoke("帮我把订单 ORD100001 退了")
            self.assertEqual(result["outcome"].kind, OutcomeKind.HANDOFF)
            self.assertIn("不能再次退款", result["outcome"].final_answer)
        self.assertEqual(len(self.actions()), before)

    def test_4_manual_review_does_not_create_proposal(self) -> None:
        before = len(self.actions())
        for _ in range(3):
            result = self.invoke("帮我把订单 ORD200002 退了")
            self.assertEqual(result["outcome"].kind, OutcomeKind.HANDOFF)
            self.assertIn("人工审核", result["outcome"].final_answer)
        self.assertEqual(len(self.actions()), before)

    def test_5_missing_order_requests_input(self) -> None:
        before = len(self.actions())
        for _ in range(3):
            result = self.invoke("帮我退一下订单")
            self.assertEqual(
                result["outcome"].kind,
                OutcomeKind.REQUEST_INPUT,
            )
            self.assertIn("订单号", result["outcome"].final_answer)
        self.assertEqual(len(self.actions()), before)

    def test_6_preview_calls_only_read_endpoints(self) -> None:
        import tools.facts_client as facts_client

        original_post = facts_client.FACTS_HTTP_CLIENT.post
        for _ in range(3):
            requested_paths: list[str] = []

            def recording_post(*args, **kwargs):
                requested_paths.append(str(args[0]))
                return original_post(*args, **kwargs)

            with patch.object(
                facts_client.FACTS_HTTP_CLIENT,
                "post",
                side_effect=recording_post,
            ):
                result = self.invoke("帮我把订单 ORD200001 退了")

            self.assertEqual(result["outcome"].kind, OutcomeKind.ANSWER)
            self.assertEqual(
                [path.rsplit("/api", 1)[-1] for path in requested_paths],
                [
                    "/v1/agent/order/facts",
                    "/v1/agent/order/refund/preview",
                ],
            )

    def test_7_refund_arrival_remains_handoff(self) -> None:
        before = len(self.actions())
        for _ in range(3):
            result = self.invoke(
                "订单 ORD100001 CLOSE 了，退款到账了吗？"
            )
            self.assertEqual(result["outcome"].kind, OutcomeKind.HANDOFF)
            self.assertEqual(_tool_names(result), [])
        self.assertEqual(len(self.actions()), before)


if __name__ == "__main__":
    unittest.main()
