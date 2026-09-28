from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from action_ledger import ActionStatus, AgentActionStore
from reconciler import reconcile_stuck_actions
from tools.refund_execute import ExecutionCertainty, RefundExecutionResult


class RefundReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-reconciler-",
        )
        self.action_path = (
            Path(self.temporary_directory.name) / "agent_actions.sqlite"
        )
        self.previous_action_path = os.environ.get("AGENT_ACTION_DB_PATH")
        os.environ["AGENT_ACTION_DB_PATH"] = str(self.action_path)
        self.store = AgentActionStore(self.action_path)
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        if self.previous_action_path is None:
            os.environ.pop("AGENT_ACTION_DB_PATH", None)
        else:
            os.environ["AGENT_ACTION_DB_PATH"] = self.previous_action_path
        self.temporary_directory.cleanup()

    def _proposal(self, *, age_seconds: int = 120) -> dict[str, object]:
        action, _ = self.store.create_or_reuse_refund_proposal(
            session_id=f"session-{len(self.store.list_actions())}",
            user_id="user-c4",
            out_trade_no=f"ORD{100001 + len(self.store.list_actions())}",
            preview={
                "orderStatus": "NORMAL",
                "teamStatus": "PROGRESS",
                "refundType": "PAID_UNFORMED",
                "refundProposalAllowed": True,
                "requiresManualReview": False,
                "orderUpdateTime": "2026-09-28T10:00:00+00:00",
                "teamUpdateTime": "2026-09-28T10:00:00+00:00",
            },
            expected_version=(
                "2026-09-28T10:00:00+00:00|"
                "2026-09-28T10:00:00+00:00"
            ),
            now=self.now - timedelta(seconds=age_seconds),
        )
        return action

    def _confirmed(self, *, age_seconds: int = 120) -> dict[str, object]:
        action = self._proposal(age_seconds=age_seconds)
        confirmed_at = self.now - timedelta(seconds=age_seconds)
        confirmed = self.store.confirm_refund_proposal(
            action_id=str(action["action_id"]),
            user_id=str(action["user_id"]),
            expected_version=int(action["version"]),
            now=confirmed_at,
        )
        self.assertTrue(confirmed)
        return self.store.get_action(str(action["action_id"])) or {}

    def _executing(self) -> dict[str, object]:
        action = self._confirmed()
        executing = self.store.transition_status(
            action_id=str(action["action_id"]),
            from_status=ActionStatus.CONFIRMED,
            to_status=ActionStatus.EXECUTING,
            expected_version=int(action["version"]),
            now=self.now - timedelta(seconds=120),
        )
        self.assertIsNotNone(executing)
        return executing or {}

    def _unknown(self) -> dict[str, object]:
        action = self._executing()
        unknown = self.store.transition_status(
            action_id=str(action["action_id"]),
            from_status=ActionStatus.EXECUTING,
            to_status=ActionStatus.UNKNOWN,
            expected_version=int(action["version"]),
            result_code="REQUEST_TIMEOUT",
            now=self.now - timedelta(seconds=120),
        )
        self.assertIsNotNone(unknown)
        return unknown or {}

    def test_stale_confirmed_fails_without_result_query(self) -> None:
        action = self._confirmed()
        with patch("reconciler.query_refund_result") as query:
            summary = reconcile_stuck_actions(now=self.now)

        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["result_code"], "CONFIRMED_NOT_EXECUTED")
        self.assertEqual(final["refund_executed"], 0)
        query.assert_not_called()

    def test_fresh_confirmed_is_not_scanned(self) -> None:
        action = self._confirmed(age_seconds=0)
        with patch("reconciler.query_refund_result") as query:
            summary = reconcile_stuck_actions(now=self.now, min_age=60)

        self.assertEqual(summary["scanned"], 0)
        self.assertEqual(
            self.store.get_action(str(action["action_id"]))["status"],
            "CONFIRMED",
        )
        query.assert_not_called()

    def test_executing_moves_to_unknown_then_succeeds(self) -> None:
        action = self._executing()
        result = RefundExecutionResult(
            certainty=ExecutionCertainty.SUCCESS,
            status="SUCCEEDED",
            result_code="REFUND_SUCCEEDED",
            refund_executed=True,
        )
        with patch("reconciler.query_refund_result", return_value=result) as query:
            summary = reconcile_stuck_actions(now=self.now)

        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertEqual(final["refund_executed"], 1)
        query.assert_called_once()

    def test_unknown_abandoned_fails(self) -> None:
        action = self._unknown()
        result = RefundExecutionResult(
            certainty=ExecutionCertainty.FAILURE,
            status="ABANDONED",
            result_code="NOT_RECEIVED_BEFORE_QUERY",
            refund_executed=False,
        )
        with patch("reconciler.query_refund_result", return_value=result):
            summary = reconcile_stuck_actions(now=self.now)

        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(
            final["result_code"],
            "NOT_RECEIVED_BEFORE_QUERY",
        )
        self.assertEqual(final["refund_executed"], 0)

    def test_processing_reaches_manual_and_is_not_queried_again(self) -> None:
        action = self._unknown()
        result = RefundExecutionResult(
            certainty=ExecutionCertainty.UNKNOWN,
            status="PROCESSING",
            result_code="REFUND_PROCESSING",
        )
        with patch("reconciler.query_refund_result", return_value=result) as query:
            for attempt in range(5):
                summary = reconcile_stuck_actions(
                    now=self.now + timedelta(seconds=attempt),
                    min_age=0,
                    max_attempts=5,
                )
            sixth = reconcile_stuck_actions(
                now=self.now + timedelta(seconds=6),
                min_age=0,
                max_attempts=5,
            )

        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(summary["manual"], 1)
        self.assertEqual(final["status"], "UNKNOWN")
        self.assertEqual(final["reconcile_attempts"], 5)
        self.assertEqual(final["needs_manual"], 1)
        self.assertEqual(sixth["scanned"], 0)
        self.assertEqual(query.call_count, 5)

        from api import _action_status_response

        response = _action_status_response(
            final,
            message="退款结果暂时无法确认，正在等待对账。",
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(payload["needs_manual"])
        self.assertEqual(
            payload["message"],
            "退款结果暂时无法自动确认，已转人工处理。",
        )

    def test_terminal_cas_does_not_overwrite_concurrent_change(self) -> None:
        action = self._unknown()
        successful = RefundExecutionResult(
            certainty=ExecutionCertainty.SUCCESS,
            status="SUCCEEDED",
            result_code="REFUND_SUCCEEDED",
            refund_executed=True,
        )

        def change_action(_: dict[str, object], **__: object):
            current = self.store.get_action(str(action["action_id"]))
            self.store.transition_status(
                action_id=str(action["action_id"]),
                from_status=ActionStatus.UNKNOWN,
                to_status=ActionStatus.FAILED,
                expected_version=int(current["version"]),
                result_code="CONCURRENT_RESULT",
                refund_executed=False,
                now=self.now,
            )
            return successful

        with patch("reconciler.query_refund_result", side_effect=change_action):
            summary = reconcile_stuck_actions(now=self.now)

        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(summary["cas_skipped"], 1)
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["result_code"], "CONCURRENT_RESULT")

    def test_schema_migrates_reconcile_columns(self) -> None:
        with closing(sqlite3.connect(self.action_path)) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_action)"
                ).fetchall()
            }
        self.assertIn("reconcile_attempts", columns)
        self.assertIn("needs_manual", columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
