from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from action_ledger import AgentActionStore
from confirmation_workflow import (
    _compile_confirmation_graph,
    confirmation_config,
    start_confirmation_workflow,
)
from tools.refund_execute import (
    ExecutionCertainty,
    RefundExecutionResult,
)


PREVIEW = {
    "orderStatus": "COMPLETE",
    "teamStatus": "PROGRESS",
    "refundType": "PAID_UNFORMED",
    "refundProposalAllowed": True,
    "requiresManualReview": False,
    "orderUpdateTime": "2026-09-27T10:00:00+08:00",
    "teamUpdateTime": "2026-09-27T10:05:00+08:00",
}
EXPECTED_VERSION = (
    "2026-09-27T10:00:00+08:00|2026-09-27T10:05:00+08:00"
)


class C3RefundWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-c3-unit-"
        )
        self.temp_path = Path(self.temporary_directory.name)
        self.action_path = self.temp_path / "actions.sqlite"
        self.checkpoint_path = self.temp_path / "checkpoints.sqlite"
        self.previous_action_path = os.environ.get("AGENT_ACTION_DB_PATH")
        os.environ["AGENT_ACTION_DB_PATH"] = str(self.action_path)
        self.store = AgentActionStore(self.action_path)

    def tearDown(self) -> None:
        if self.previous_action_path is None:
            os.environ.pop("AGENT_ACTION_DB_PATH", None)
        else:
            os.environ["AGENT_ACTION_DB_PATH"] = self.previous_action_path
        self.temporary_directory.cleanup()

    def create_action(
        self,
        *,
        session_id: str | None = None,
    ) -> dict[str, object]:
        action, created = self.store.create_or_reuse_refund_proposal(
            session_id=session_id or f"c3-{uuid.uuid4().hex}",
            user_id="demo-user",
            out_trade_no="ORD-C3-TEST",
            preview=PREVIEW,
            expected_version=EXPECTED_VERSION,
        )
        self.assertTrue(created)
        start_confirmation_workflow(self.checkpoint_path, action)
        return action

    def resume(
        self,
        action: dict[str, object],
        *,
        execute_side_effect: list[RefundExecutionResult],
        facts_side_effect: list[tuple[bool, str | None]],
    ) -> tuple[dict[str, object], list[str]]:
        observed_keys: list[str] = []

        def execute(current, *, request_id=""):
            del request_id
            observed_keys.append(str(current["idempotency_key"]))
            return execute_side_effect.pop(0)

        connection = sqlite3.connect(
            self.checkpoint_path,
            check_same_thread=False,
        )
        graph = _compile_confirmation_graph(SqliteSaver(connection))
        try:
            with (
                patch(
                    "confirmation_workflow.execute_refund",
                    side_effect=execute,
                ),
                patch(
                    "confirmation_workflow._order_status",
                    side_effect=facts_side_effect,
                ),
            ):
                result = graph.invoke(
                    Command(
                        resume={
                            "credential_validated": True,
                            "request_id": "c3-unit-request",
                        }
                    ),
                    config=confirmation_config(str(action["action_id"])),
                )
        finally:
            connection.close()
        return result, observed_keys

    def test_explicit_failure_and_close_stays_failed(self) -> None:
        action = self.create_action()
        result, keys = self.resume(
            action,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.FAILURE,
                    status="FAILED",
                    result_code="BUSINESS_REJECTED",
                )
            ],
            facts_side_effect=[(True, "CLOSE")],
        )
        current = self.store.get_action(str(action["action_id"]))
        self.assertEqual(result["final_status"], "FAILED")
        self.assertEqual(current["status"], "FAILED")
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["result_code"], "BUSINESS_REJECTED")
        self.assertEqual(current["refund_executed"], 0)
        self.assertIn("订单已经是关闭状态，本次没有重复退款", result["message"])
        self.assertEqual(keys, [action["idempotency_key"]])

    def test_timeout_retries_once_with_same_key_then_succeeds(self) -> None:
        action = self.create_action()
        result, keys = self.resume(
            action,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.UNKNOWN,
                    result_code="REQUEST_TIMEOUT",
                ),
                RefundExecutionResult(
                    certainty=ExecutionCertainty.SUCCESS,
                    status="SUCCEEDED",
                    result_code="REFUND_SUCCEEDED",
                    refund_executed=True,
                    idempotent_replay=True,
                ),
            ],
            facts_side_effect=[(True, "CLOSE")],
        )
        current = self.store.get_action(str(action["action_id"]))
        self.assertEqual(result["final_status"], "SUCCEEDED")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(current["version"], 5)
        self.assertEqual(current["result_code"], "REFUND_SUCCEEDED")
        self.assertEqual(current["refund_executed"], 1)
        self.assertEqual(keys, [action["idempotency_key"]] * 2)

    def test_persistent_uncertainty_stops_after_one_retry(self) -> None:
        action = self.create_action()
        result, keys = self.resume(
            action,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.UNKNOWN,
                    result_code="REQUEST_TIMEOUT",
                ),
                RefundExecutionResult(
                    certainty=ExecutionCertainty.UNKNOWN,
                    result_code="REQUEST_TIMEOUT",
                ),
            ],
            facts_side_effect=[(True, "NORMAL")],
        )
        current = self.store.get_action(str(action["action_id"]))
        self.assertEqual(result["final_status"], "UNKNOWN")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(current["status"], "UNKNOWN")
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["result_code"], "REQUEST_TIMEOUT")
        self.assertIsNone(current["refund_executed"])
        self.assertEqual(keys, [action["idempotency_key"]] * 2)

    def test_version_changed_without_close_fails(self) -> None:
        action = self.create_action()
        result, keys = self.resume(
            action,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.FAILURE,
                    status="FAILED",
                    result_code="VERSION_CHANGED",
                )
            ],
            facts_side_effect=[(True, "NORMAL")],
        )
        current = self.store.get_action(str(action["action_id"]))
        self.assertEqual(result["final_status"], "FAILED")
        self.assertEqual(current["status"], "FAILED")
        self.assertEqual(current["version"], 4)
        self.assertEqual(current["result_code"], "VERSION_CHANGED")
        self.assertEqual(current["refund_executed"], 0)
        self.assertIn("重新发起", result["message"])
        self.assertEqual(keys, [action["idempotency_key"]])

    def test_success_is_terminal_when_facts_query_fails(self) -> None:
        action = self.create_action()
        result, keys = self.resume(
            action,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.SUCCESS,
                    status="SUCCEEDED",
                    result_code="REFUND_SUCCEEDED",
                    refund_executed=True,
                )
            ],
            facts_side_effect=[(False, None)],
        )
        current = self.store.get_action(str(action["action_id"]))
        self.assertEqual(result["final_status"], "SUCCEEDED")
        self.assertEqual(current["status"], "SUCCEEDED")
        self.assertEqual(current["result_code"], "REFUND_SUCCEEDED")
        self.assertEqual(current["refund_executed"], 1)
        self.assertIn("暂时无法查询最新订单状态", result["message"])
        self.assertEqual(keys, [action["idempotency_key"]])

    def test_two_sessions_same_order_have_distinct_terminal_results(self) -> None:
        first = self.create_action(session_id=f"c3-first-{uuid.uuid4().hex}")
        second = self.create_action(session_id=f"c3-second-{uuid.uuid4().hex}")
        first_result, first_keys = self.resume(
            first,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.SUCCESS,
                    status="SUCCEEDED",
                    result_code="REFUND_SUCCEEDED",
                    refund_executed=True,
                )
            ],
            facts_side_effect=[(True, "CLOSE")],
        )
        second_result, second_keys = self.resume(
            second,
            execute_side_effect=[
                RefundExecutionResult(
                    certainty=ExecutionCertainty.FAILURE,
                    status="FAILED",
                    result_code="VERSION_CHANGED",
                    refund_executed=False,
                )
            ],
            facts_side_effect=[(True, "CLOSE")],
        )
        first_action = self.store.get_action(str(first["action_id"]))
        second_action = self.store.get_action(str(second["action_id"]))
        self.assertEqual(first_result["final_status"], "SUCCEEDED")
        self.assertEqual(second_result["final_status"], "FAILED")
        self.assertEqual(first_action["refund_executed"], 1)
        self.assertEqual(second_action["refund_executed"], 0)
        self.assertIn(
            "订单已经是关闭状态，本次没有重复退款",
            second_result["message"],
        )
        self.assertNotIn("退款失败", second_result["message"])
        self.assertEqual(first_keys, [first["idempotency_key"]])
        self.assertEqual(second_keys, [second["idempotency_key"]])


if __name__ == "__main__":
    unittest.main()
