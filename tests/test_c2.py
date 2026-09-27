from __future__ import annotations

import concurrent.futures
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt

from action_ledger import AgentActionStore, confirmation_credential
from confirmation_workflow import start_confirmation_workflow
from tests.test_b2 import (
    collect_sse,
    free_port,
    stop_process,
    wait_for_port,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.getenv("RUN_C2_INTEGRATION") == "1"

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


@unittest.skipUnless(
    RUN_INTEGRATION,
    "设置 RUN_C2_INTEGRATION=1 后运行 C2 集成测试",
)
class C2ConfirmationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-c2-",
            dir=PROJECT_ROOT,
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.action_path = cls.temp_path / "agent_actions.sqlite"
        cls.checkpoint_path = cls.temp_path / "checkpoints.sqlite"
        cls.store = AgentActionStore(cls.action_path)
        cls.fake_port = free_port()
        cls.api_port = free_port()
        cls.jwt_secret = secrets.token_urlsafe(32)
        cls.confirm_secret = secrets.token_urlsafe(32)
        cls.user_a_token = cls._token("demo-user")
        cls.user_b_token = cls._token("user-B")
        cls.fake_process: subprocess.Popen[bytes] | None = None
        cls.api_process: subprocess.Popen[bytes] | None = None
        cls.api_log_path = cls.temp_path / "api.log"
        cls.api_log = cls.api_log_path.open("w+b")

        environment = os.environ.copy()
        environment["FAKE_JAVA_DELAY_SECONDS"] = "0"
        cls.fake_process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "uvicorn",
                "fake_java.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.fake_port),
                "--log-level",
                "error",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_port(cls.fake_process, cls.fake_port)

        launcher = (
            "import os,uvicorn,api; "
            "os.environ['JAVA_BASE_URL']=''; "
            f"os.environ['FAKE_JAVA_BASE_URL']='http://127.0.0.1:{cls.fake_port}'; "
            f"os.environ['CHECKPOINT_DB_PATH']={str(cls.checkpoint_path)!r}; "
            f"os.environ['AGENT_ACTION_DB_PATH']={str(cls.action_path)!r}; "
            f"os.environ['JWT_SECRET']={cls.jwt_secret!r}; "
            f"os.environ['ACTION_CONFIRM_SECRET']={cls.confirm_secret!r}; "
            f"uvicorn.run(api.app,host='127.0.0.1',port={cls.api_port},"
            "log_level='info')"
        )
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        cls.api_process = subprocess.Popen(
            [sys.executable, "-B", "-c", launcher],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=cls.api_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_port(cls.api_process, cls.api_port)
        cls.chat_url = (
            f"http://127.0.0.1:{cls.api_port}/api/v1/chat/stream"
        )
        cls.action_url = f"http://127.0.0.1:{cls.api_port}/v1/actions"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_process(cls.api_process)
        stop_process(cls.fake_process)
        cls.api_log.close()
        cls.temporary_directory.cleanup()

    @classmethod
    def _token(cls, user_id: str) -> str:
        return jwt.encode(
            {"sub": user_id, "exp": int(time.time()) + 3600},
            cls.jwt_secret,
            algorithm="HS256",
        )

    def create_action(
        self,
        *,
        session_id: str | None = None,
        user_id: str = "demo-user",
        out_trade_no: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        action, created = self.store.create_or_reuse_refund_proposal(
            session_id=session_id or f"c2-{uuid.uuid4().hex}",
            user_id=user_id,
            out_trade_no=out_trade_no or "ORD100001",
            preview=PREVIEW,
            expected_version=EXPECTED_VERSION,
            now=now,
        )
        if created:
            start_confirmation_workflow(self.checkpoint_path, action)
        return action, created

    def confirm(
        self,
        action: dict[str, Any],
        *,
        token: str | None = None,
        credential: str | None = None,
    ) -> httpx.Response:
        submitted = credential or confirmation_credential(
            action,
            secret=self.confirm_secret,
        )
        with httpx.Client(timeout=10, trust_env=False) as client:
            return client.post(
                f"{self.action_url}/{action['action_id']}/confirm",
                json={"credential": submitted},
                headers={
                    "Authorization": f"Bearer {token or self.user_a_token}"
                },
            )

    def test_1_normal_confirmation_only_updates_ledger(self) -> None:
        action, created = self.create_action()
        self.assertTrue(created)
        self.assertEqual(action["status"], "PROPOSED")
        self.assertTrue(str(action["idempotency_key"]).startswith("refund_"))
        self.assertEqual(len(str(action["args_hash"])), 64)
        self.assertEqual(action["version"], 1)

        response = self.confirm(action)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "SUCCEEDED")
        self.assertTrue(response.json()["executed"])
        confirmed = self.store.get_action(str(action["action_id"]))
        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed["status"], "SUCCEEDED")
        self.assertEqual(confirmed["version"], 4)

    def test_2_chat_confirmation_does_not_resume_action(self) -> None:
        session_id = f"chat-confirm-{uuid.uuid4().hex}"
        proposal_events, _ = collect_sse(
            self.chat_url,
            {
                "session_id": session_id,
                "message": "帮我把订单 ORD200001 退了",
            },
            token=self.user_a_token,
        )
        self.assertEqual(proposal_events[-1]["data"]["kind"], "ANSWER")
        proposed_index = next(
            index
            for index, event in enumerate(proposal_events)
            if event["event"] == "action_proposed"
        )
        self.assertLess(proposed_index, len(proposal_events) - 1)
        proposed = proposal_events[proposed_index]["data"]
        actions = [
            item
            for item in self.store.list_actions()
            if item["session_id"] == session_id
        ]
        self.assertEqual(len(actions), 1)
        action = actions[0]
        credential = proposed["credential"]
        self.assertEqual(proposed["action_id"], action["action_id"])
        self.assertEqual(proposed["expires_at"], action["expires_at"])
        self.assertEqual(proposed["preview"], PREVIEW)
        self.assertNotIn(
            credential,
            proposal_events[-1]["data"]["answer"],
        )
        self.assertNotIn(
            str(action["action_id"]),
            proposal_events[-1]["data"]["answer"],
        )

        events, _ = collect_sse(
            self.chat_url,
            {"session_id": session_id, "message": "我确认"},
            token=self.user_a_token,
        )

        self.assertEqual(events[-1]["event"], "final")
        self.assertEqual(
            self.store.get_action(str(action["action_id"]))["status"],
            "PROPOSED",
        )
        from agent import close_order_agent, create_order_agent

        checkpoint_reader = create_order_agent(self.checkpoint_path)
        try:
            snapshot = checkpoint_reader.get_state(
                {"configurable": {"thread_id": session_id}}
            )
            message_history = "\n".join(
                str(message.content)
                for message in snapshot.values.get("messages", [])
            )
        finally:
            close_order_agent(checkpoint_reader)
        self.assertNotIn(credential, message_history)

        confirmed = self.confirm(action, credential=credential)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

        credential_bytes = credential.encode("ascii")
        checkpoint_files = list(
            self.temp_path.glob("checkpoints.sqlite*")
        )
        self.assertTrue(checkpoint_files)
        self.assertTrue(
            all(
                credential_bytes not in path.read_bytes()
                for path in checkpoint_files
            )
        )
        self.assertNotIn(credential_bytes, self.api_log_path.read_bytes())

    def test_11_changed_args_hash_is_rejected(self) -> None:
        action, _ = self.create_action()
        credential = confirmation_credential(
            action,
            secret=self.confirm_secret,
        )
        with sqlite3.connect(self.action_path) as connection:
            connection.execute(
                "UPDATE agent_action SET args_hash = ? WHERE action_id = ?",
                ("0" * 64, action["action_id"]),
            )
            connection.commit()
        response = self.confirm(action, credential=credential)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "ACTION_ARGS_CHANGED",
        )

    def test_12_reused_proposal_emits_action_event(self) -> None:
        session_id = f"sse-reuse-{uuid.uuid4().hex}"
        payload = {
            "session_id": session_id,
            "message": "帮我把订单 ORD200001 退了",
        }
        first_events, _ = collect_sse(
            self.chat_url,
            payload,
            token=self.user_a_token,
        )
        second_events, _ = collect_sse(
            self.chat_url,
            payload,
            token=self.user_a_token,
        )
        first = next(
            event["data"]
            for event in first_events
            if event["event"] == "action_proposed"
        )
        second = next(
            event["data"]
            for event in second_events
            if event["event"] == "action_proposed"
        )
        self.assertEqual(first["action_id"], second["action_id"])
        self.assertEqual(first["credential"], second["credential"])
        actions = [
            action
            for action in self.store.list_actions()
            if action["session_id"] == session_id
        ]
        self.assertEqual(len(actions), 1)

    def test_3_expired_credential_is_rejected(self) -> None:
        action, _ = self.create_action(
            now=datetime.now(timezone.utc) - timedelta(minutes=6)
        )
        response = self.confirm(action)
        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(
            self.store.get_action(str(action["action_id"]))["status"],
            "PROPOSED",
        )

    def test_4_tampered_credential_is_rejected(self) -> None:
        action, _ = self.create_action()
        credential = confirmation_credential(
            action,
            secret=self.confirm_secret,
        )
        replacement = "0" if credential[-1] != "0" else "1"
        response = self.confirm(
            action,
            credential=f"{credential[:-1]}{replacement}",
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_5_cross_user_confirmation_is_rejected(self) -> None:
        action, _ = self.create_action()
        response = self.confirm(action, token=self.user_b_token)
        self.assertEqual(response.status_code, 403, response.text)

    def test_6_repeated_confirmation_is_rejected(self) -> None:
        action, _ = self.create_action()
        first = self.confirm(action)
        second = self.confirm(action)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["status"], "SUCCEEDED")
        self.assertIn("已完成", second.json()["message"])

    def test_7_unexpired_proposal_is_reused(self) -> None:
        session_id = f"reuse-{uuid.uuid4().hex}"
        out_trade_no = f"ORD-{uuid.uuid4().hex}"
        first, first_created = self.create_action(
            session_id=session_id,
            out_trade_no=out_trade_no,
        )
        second, second_created = self.create_action(
            session_id=session_id,
            out_trade_no=out_trade_no,
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["action_id"], second["action_id"])

    def test_8_expired_proposal_can_be_recreated(self) -> None:
        session_id = f"expired-reuse-{uuid.uuid4().hex}"
        out_trade_no = f"ORD-{uuid.uuid4().hex}"
        expired, _ = self.create_action(
            session_id=session_id,
            out_trade_no=out_trade_no,
            now=datetime.now(timezone.utc) - timedelta(minutes=6),
        )
        replacement, created = self.create_action(
            session_id=session_id,
            out_trade_no=out_trade_no,
        )
        self.assertTrue(created)
        self.assertNotEqual(expired["action_id"], replacement["action_id"])

    def test_9_concurrent_http_confirmation_has_one_success(self) -> None:
        action, _ = self.create_action()

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(
                pool.map(lambda _: self.confirm(action), range(10))
            )

        self.assertGreaterEqual(
            sum(response.status_code == 200 for response in responses),
            1,
        )
        self.assertTrue(
            all(response.status_code in {200, 202} for response in responses)
        )
        confirmed = self.store.get_action(str(action["action_id"]))
        self.assertEqual(confirmed["status"], "SUCCEEDED")
        self.assertEqual(confirmed["version"], 4)

    def test_10_chat_remains_available_while_confirmation_waits(self) -> None:
        session_id = f"chat-available-{uuid.uuid4().hex}"
        action, _ = self.create_action(session_id=session_id)

        events, _ = collect_sse(
            self.chat_url,
            {
                "session_id": session_id,
                "message": "活动 100123 还有效吗",
            },
            token=self.user_a_token,
        )

        self.assertEqual(events[-1]["data"]["kind"], "ANSWER")
        self.assertEqual(
            self.store.get_action(str(action["action_id"]))["status"],
            "PROPOSED",
        )


if __name__ == "__main__":
    unittest.main()
