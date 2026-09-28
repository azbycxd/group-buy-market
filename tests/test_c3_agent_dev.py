from __future__ import annotations

import concurrent.futures
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import jwt
from dotenv import dotenv_values

from action_ledger import AgentActionStore
from reconciler import reconcile_stuck_actions
from tests.test_b2 import collect_sse, free_port, stop_process, wait_for_port


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVA_PROJECT = Path(
    os.getenv(
        "AGENT_DEV_JAVA_PROJECT",
        str(PROJECT_ROOT.parent / "group-buy-market-jiusi"),
    )
)
RESET_SCRIPT = JAVA_PROJECT / "scripts" / "agent-dev" / "reset-agent-dev.ps1"
JAVA_DEV_CONFIG = (
    JAVA_PROJECT
    / "group-buy-market-app"
    / "src"
    / "main"
    / "resources"
    / "application-dev.yml"
)
POWERSHELL_7 = Path(
    os.getenv(
        "POWERSHELL_7",
        str(
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "native"
            / "powershell"
            / "pwsh.exe"
        ),
    )
)
RUN_AGENT_DEV = os.getenv("RUN_C3_AGENT_DEV") == "1"
REAL_JAVA_BASE_URL = os.getenv(
    "C3_JAVA_BASE_URL",
    "http://127.0.0.1:8091",
)


def _agent_dev_mysql_password() -> str:
    config = JAVA_DEV_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s*password:\s*(\S+)\s*$", config, re.MULTILINE)
    if match is None:
        raise RuntimeError("无法从 application-dev.yml 读取 MySQL 密码")
    return match.group(1)


def _reset_agent_dev() -> None:
    environment = os.environ.copy()
    environment["AGENT_DEV_MYSQL_PASSWORD"] = _agent_dev_mysql_password()
    result = subprocess.run(
        [
            str(POWERSHELL_7),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RESET_SCRIPT),
        ],
        cwd=JAVA_PROJECT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agent-dev reset 失败: {result.stdout[-500:]} {result.stderr[-500:]}"
        )


def _mysql_scalar(query: str) -> str:
    mysql = Path(
        os.getenv(
            "MYSQL_CLIENT",
            r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe",
        )
    )
    environment = os.environ.copy()
    environment["MYSQL_PWD"] = _agent_dev_mysql_password()
    result = subprocess.run(
        [
            str(mysql),
            "--protocol=tcp",
            "-h",
            "127.0.0.1",
            "-P",
            "13306",
            "-u",
            "root",
            "-N",
            "-B",
            "-e",
            f"USE group_buy_market_agent_test; {query}",
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


def _action_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        event["data"]
        for event in events
        if event["event"] == "action_proposed"
    )


class _RefundRecordingProxy:
    def __init__(
        self,
        upstream: str,
        *,
        refund_delay_mode: str | None = None,
        delay_seconds: float = 0.5,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.refund_delay_mode = refund_delay_mode
        self.delay_seconds = delay_seconds
        self.port = free_port()
        self.refund_keys: list[str] = []
        self.result_keys: list[str] = []
        self._refund_requests = 0
        self.refund_received = threading.Event()
        self.refund_forwarded = threading.Event()
        self.release_refund = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                if self.path == "/api/v1/agent/order/refund":
                    body = json.loads(raw_body)
                    owner.refund_keys.append(str(body["idempotencyKey"]))
                    owner._refund_requests += 1
                    owner.refund_received.set()
                    if (
                        owner.refund_delay_mode == "before_forward"
                        and owner._refund_requests == 1
                    ):
                        time.sleep(owner.delay_seconds)
                    if (
                        owner.refund_delay_mode == "before_forward_hold"
                        and owner._refund_requests == 1
                    ):
                        owner.release_refund.wait(timeout=20)
                elif self.path == "/api/v1/agent/order/refund/result":
                    body = json.loads(raw_body)
                    owner.result_keys.append(str(body["idempotencyKey"]))
                if (
                    owner.refund_delay_mode == "service_unavailable"
                    and self.path in {
                        "/api/v1/agent/order/refund",
                        "/api/v1/agent/order/refund/result",
                    }
                ):
                    payload = b'{"code":"SERVICE_UNAVAILABLE"}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                forwarded_headers = {
                    name: value
                    for name, value in self.headers.items()
                    if name.lower() not in {"host", "content-length"}
                }
                with httpx.Client(timeout=10, trust_env=False) as client:
                    response = client.post(
                        f"{owner.upstream}{self.path}",
                        content=raw_body,
                        headers=forwarded_headers,
                    )
                if self.path == "/api/v1/agent/order/refund":
                    owner.refund_forwarded.set()
                if (
                    self.path == "/api/v1/agent/order/refund"
                    and owner.refund_delay_mode == "after_forward"
                    and owner._refund_requests == 1
                ):
                    time.sleep(owner.delay_seconds)
                if (
                    self.path == "/api/v1/agent/order/refund"
                    and owner.refund_delay_mode == "after_forward_hold"
                    and owner._refund_requests == 1
                ):
                    owner.release_refund.wait(timeout=20)
                try:
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", response.headers.get(
                        "content-type", "application/json"
                    ))
                    self.send_header(
                        "Content-Length",
                        str(len(response.content)),
                    )
                    self.end_headers()
                    self.wfile.write(response.content)
                except OSError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.release_refund.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(
    RUN_AGENT_DEV,
    "设置 RUN_C3_AGENT_DEV=1 后运行真实 agent-dev 集成测试",
)
class C3AgentDevIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-c3-real-",
            dir=PROJECT_ROOT,
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        configured = dotenv_values(PROJECT_ROOT / ".env")
        cls.jwt_secret = str(configured["JWT_SECRET"])
        cls.previous_java_environment = {
            name: os.environ.get(name)
            for name in (
                "JAVA_BASE_URL",
                "JAVA_INTERNAL_JWT_SECRET",
                "JAVA_INTERNAL_JWT_ISSUER",
                "JAVA_INTERNAL_JWT_AUDIENCE",
            )
        }
        os.environ["JAVA_BASE_URL"] = REAL_JAVA_BASE_URL
        for name in (
            "JAVA_INTERNAL_JWT_SECRET",
            "JAVA_INTERNAL_JWT_ISSUER",
            "JAVA_INTERNAL_JWT_AUDIENCE",
        ):
            os.environ[name] = str(configured[name])
        cls.confirm_secret = secrets.token_urlsafe(32)
        cls.api_process: subprocess.Popen[bytes] | None = None
        cls.api_log = None
        cls._start_api(REAL_JAVA_BASE_URL)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_api()
        for name, value in cls.previous_java_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        cls.temporary_directory.cleanup()

    @classmethod
    def _start_api(
        cls,
        java_base_url: str,
        *,
        refund_timeout: float = 5.0,
    ) -> None:
        cls.api_port = free_port()
        cls.action_path = cls.temp_path / f"actions-{cls.api_port}.sqlite"
        cls.checkpoint_path = cls.temp_path / f"checkpoints-{cls.api_port}.sqlite"
        log_path = cls.temp_path / f"api-{cls.api_port}.log"
        cls.api_log_path = log_path
        cls.api_log = log_path.open("w+b")
        launcher = (
            "import os,uvicorn,api; "
            f"os.environ['JAVA_BASE_URL']={java_base_url!r}; "
            f"os.environ['JAVA_REFUND_TIMEOUT_SECONDS']={str(refund_timeout)!r}; "
            "os.environ['JAVA_REFUND_RESULT_TIMEOUT_SECONDS']='2'; "
            f"uvicorn.run(api.app,host='127.0.0.1',port={cls.api_port},"
            "log_level='info')"
        )
        environment = os.environ.copy()
        environment["CHECKPOINT_DB_PATH"] = str(cls.checkpoint_path)
        environment["AGENT_ACTION_DB_PATH"] = str(cls.action_path)
        environment["ACTION_CONFIRM_SECRET"] = cls.confirm_secret
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
        cls.chat_url = f"http://127.0.0.1:{cls.api_port}/api/v1/chat/stream"
        cls.action_url = f"http://127.0.0.1:{cls.api_port}/v1/actions"
        cls.store = AgentActionStore(cls.action_path)

    @classmethod
    def _stop_api(cls) -> None:
        stop_process(cls.api_process)
        cls.api_process = None
        if cls.api_log is not None:
            cls.api_log.close()
            cls.api_log = None

    @classmethod
    def _restart_api(
        cls,
        java_base_url: str,
        *,
        refund_timeout: float,
    ) -> None:
        cls._stop_api()
        cls._start_api(java_base_url, refund_timeout=refund_timeout)

    def setUp(self) -> None:
        _reset_agent_dev()

    def _token(self, user_id: str) -> str:
        return jwt.encode(
            {"sub": user_id, "exp": int(time.time()) + 3600},
            self.jwt_secret,
            algorithm="HS256",
        )

    def _proposal(
        self,
        user_id: str,
        out_trade_no: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        events, _ = collect_sse(
            self.chat_url,
            {
                "session_id": f"c3-real-{uuid.uuid4().hex}",
                "message": f"帮我把订单 {out_trade_no} 退了",
            },
            token=self._token(user_id),
            timeout=40,
        )
        self.assertTrue(
            any(item["event"] == "action_proposed" for item in events),
            (
                events,
                self.api_log_path.read_text(
                    encoding="utf-8", errors="replace"
                )[-4000:],
            ),
        )
        event = _action_event(events)
        action = self.store.get_action(str(event["action_id"]))
        self.assertIsNotNone(action)
        return event, action, events

    def _confirm(
        self,
        event: dict[str, Any],
        user_id: str,
        *,
        timeout: float = 20,
    ) -> httpx.Response:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            return client.post(
                f"{self.action_url}/{event['action_id']}/confirm",
                json={"credential": event["credential"]},
                headers={"Authorization": f"Bearer {self._token(user_id)}"},
            )

    def test_1_unpaid_executes_and_reconciles(self) -> None:
        event, action, _ = self._proposal("agent_c3_unpaid", "930000000001")
        response = self._confirm(event, "agent_c3_unpaid")
        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "SUCCEEDED")
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertEqual(final["refund_executed"], 1)

    def test_2_paid_unformed_executes_and_reconciles(self) -> None:
        event, action, _ = self._proposal(
            "agent_c3_paid_unformed", "930000000002"
        )
        response = self._confirm(event, "agent_c3_paid_unformed")
        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "SUCCEEDED")
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertEqual(final["refund_executed"], 1)

    def test_3_paid_formed_remains_non_executable(self) -> None:
        before = len(self.store.list_actions())
        events, _ = collect_sse(
            self.chat_url,
            {
                "session_id": f"c3-formed-{uuid.uuid4().hex}",
                "message": "帮我把订单 930000000003 退了",
            },
            token=self._token("agent_c3_paid_formed"),
            timeout=40,
        )
        self.assertFalse(any(e["event"] == "action_proposed" for e in events))
        self.assertEqual(events[-1]["data"]["kind"], "HANDOFF")
        self.assertEqual(len(self.store.list_actions()), before)

    def test_4_two_tabs_execute_only_once(self) -> None:
        proxy = _RefundRecordingProxy(REAL_JAVA_BASE_URL)
        proxy.start()
        try:
            self._restart_api(
                f"http://127.0.0.1:{proxy.port}",
                refund_timeout=5.0,
            )
            event, action, _ = self._proposal(
                "agent_c3_unpaid", "930000000001"
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(
                    executor.map(
                        lambda _: self._confirm(event, "agent_c3_unpaid"),
                        range(2),
                    )
                )
            self.assertTrue(all(r.status_code in {200, 202} for r in responses))
            self.assertTrue(
                all(r.json()["status"] in {
                    "PROPOSED", "CONFIRMED", "EXECUTING", "UNKNOWN", "SUCCEEDED"
                }
                    for r in responses)
            )
            self.assertFalse(
                any(r.json()["status"] == "FAILED" for r in responses)
            )
            final = self.store.get_action(str(action["action_id"]))
            self.assertEqual(final["status"], "SUCCEEDED")
            self.assertEqual(final["version"], 4)
            self.assertEqual(proxy.refund_keys, [action["idempotency_key"]])
        finally:
            proxy.close()
            self._restart_api(REAL_JAVA_BASE_URL, refund_timeout=5.0)

    def test_5_java_failure_but_close_stays_failed(self) -> None:
        event, action, _ = self._proposal("agent_c3_unpaid", "930000000001")
        from tools.facts_client import _request_target

        base_url, headers, _ = _request_target(
            user_id="agent_c3_unpaid",
            request_id=f"external-{uuid.uuid4().hex}",
        )
        preview = json.loads(str(action["preview_json"]))
        with httpx.Client(timeout=10, trust_env=False) as client:
            external = client.post(
                f"{base_url}/api/v1/agent/order/refund",
                headers=headers,
                json={
                    "outTradeNo": action["out_trade_no"],
                    "idempotencyKey": f"external_{uuid.uuid4().hex}",
                    "expectedVersion": action["expected_version"],
                    "expectedRefundType": preview["refundType"],
                },
            )
        self.assertEqual(external.status_code, 200, external.text)
        self.assertEqual(external.json()["data"]["status"], "SUCCEEDED")

        response = self._confirm(event, "agent_c3_unpaid")
        final = self.store.get_action(str(action["action_id"]))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["result_code"], "VERSION_CHANGED")
        self.assertEqual(final["refund_executed"], 0)
        self.assertIn(
            "订单已经是关闭状态，本次没有重复退款",
            response.json()["message"],
        )

    def test_6_delayed_request_is_abandoned_without_refund(self) -> None:
        proxy = _RefundRecordingProxy(
            REAL_JAVA_BASE_URL,
            refund_delay_mode="before_forward",
        )
        proxy.start()
        try:
            self._restart_api(
                f"http://127.0.0.1:{proxy.port}",
                refund_timeout=0.1,
            )
            event, action, _ = self._proposal(
                "agent_c3_unpaid", "930000000001"
            )
            response = self._confirm(event, "agent_c3_unpaid", timeout=20)
            time.sleep(0.7)
            final = self.store.get_action(str(action["action_id"]))
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(final["status"], "FAILED")
            self.assertEqual(
                final["result_code"],
                "NOT_RECEIVED_BEFORE_QUERY",
            )
            self.assertEqual(final["refund_executed"], 0)
            self.assertIn("退款请求没有送达", response.json()["message"])
            self.assertEqual(len(proxy.refund_keys), 1)
            self.assertEqual(proxy.refund_keys[0], action["idempotency_key"])
            self.assertEqual(proxy.result_keys, [action["idempotency_key"]])
            self.assertEqual(
                _mysql_scalar(
                    "SELECT status FROM group_buy_order_list "
                    "WHERE out_trade_no='930000000001'"
                ),
                "0",
            )
            self.assertEqual(
                _mysql_scalar("SELECT COUNT(*) FROM notify_task"),
                "0",
            )
        finally:
            proxy.close()
            self._restart_api(REAL_JAVA_BASE_URL, refund_timeout=5.0)

    def test_7_lost_response_reconciles_success_without_second_write(
        self,
    ) -> None:
        proxy = _RefundRecordingProxy(
            REAL_JAVA_BASE_URL,
            refund_delay_mode="after_forward",
            delay_seconds=2.0,
        )
        proxy.start()
        try:
            self._restart_api(
                f"http://127.0.0.1:{proxy.port}",
                refund_timeout=1.0,
            )
            event, action, _ = self._proposal(
                "agent_c3_unpaid", "930000000001"
            )
            response = self._confirm(event, "agent_c3_unpaid", timeout=20)
            time.sleep(0.7)
            final = self.store.get_action(str(action["action_id"]))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(final["status"], "SUCCEEDED")
            self.assertEqual(final["result_code"], "REFUND_SUCCEEDED")
            self.assertEqual(final["refund_executed"], 1)
            self.assertEqual(proxy.refund_keys, [action["idempotency_key"]])
            self.assertEqual(proxy.result_keys, [action["idempotency_key"]])
            self.assertEqual(
                _mysql_scalar(
                    "SELECT status FROM group_buy_order_list "
                    "WHERE out_trade_no='930000000001'"
                ),
                "2",
            )
            self.assertEqual(
                _mysql_scalar("SELECT COUNT(*) FROM notify_task"),
                "1",
            )
        finally:
            proxy.close()
            self._restart_api(REAL_JAVA_BASE_URL, refund_timeout=5.0)

    def test_8_two_sessions_same_order_have_one_success(self) -> None:
        first_event, first_action, _ = self._proposal(
            "agent_c3_unpaid", "930000000001"
        )
        second_event, second_action, _ = self._proposal(
            "agent_c3_unpaid", "930000000001"
        )

        first_response = self._confirm(first_event, "agent_c3_unpaid")
        second_response = self._confirm(second_event, "agent_c3_unpaid")
        first_final = self.store.get_action(str(first_action["action_id"]))
        second_final = self.store.get_action(str(second_action["action_id"]))

        self.assertEqual(first_response.status_code, 200, first_response.text)
        self.assertEqual(second_response.status_code, 409, second_response.text)
        self.assertEqual(first_final["status"], "SUCCEEDED")
        self.assertEqual(first_final["refund_executed"], 1)
        self.assertEqual(second_final["status"], "FAILED")
        self.assertEqual(second_final["refund_executed"], 0)
        self.assertIn(
            "订单已经是关闭状态，本次没有重复退款",
            second_response.json()["message"],
        )
        self.assertNotIn("退款失败", second_response.json()["message"])

    def _kill_and_reconcile(
        self,
        *,
        delay_mode: str,
    ) -> tuple[dict[str, Any], dict[str, int], _RefundRecordingProxy]:
        proxy = _RefundRecordingProxy(
            REAL_JAVA_BASE_URL,
            refund_delay_mode=delay_mode,
        )
        proxy.start()
        self._restart_api(
            f"http://127.0.0.1:{proxy.port}",
            refund_timeout=10.0,
        )
        event, action, _ = self._proposal(
            "agent_c3_unpaid",
            "930000000001",
        )
        action_path = self.action_path
        store = AgentActionStore(action_path)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self._confirm,
            event,
            "agent_c3_unpaid",
            timeout=30,
        )
        synchronization = (
            proxy.refund_forwarded
            if delay_mode == "after_forward_hold"
            else proxy.refund_received
        )
        self.assertTrue(synchronization.wait(timeout=15))
        executing = store.get_action(str(action["action_id"]))
        self.assertEqual(executing["status"], "EXECUTING")
        self._stop_api()
        environment = {
            "AGENT_ACTION_DB_PATH": str(action_path),
            "JAVA_BASE_URL": f"http://127.0.0.1:{proxy.port}",
            "JAVA_REFUND_RESULT_TIMEOUT_SECONDS": "5",
        }
        with patch.dict(os.environ, environment):
            summary = reconcile_stuck_actions(
                now=datetime.now(timezone.utc),
                min_age=0,
            )
        proxy.release_refund.set()
        try:
            future.result(timeout=10)
        except Exception:
            pass
        executor.shutdown(wait=True)
        final = store.get_action(str(action["action_id"]))
        return final, summary, proxy

    def test_9_kill_after_java_execution_reconciles_success(self) -> None:
        proxy: _RefundRecordingProxy | None = None
        try:
            final, summary, proxy = self._kill_and_reconcile(
                delay_mode="after_forward_hold",
            )
            self.assertEqual(summary["succeeded"], 1)
            self.assertEqual(final["status"], "SUCCEEDED")
            self.assertEqual(final["result_code"], "REFUND_SUCCEEDED")
            self.assertEqual(final["refund_executed"], 1)
            self.assertEqual(len(proxy.refund_keys), 1)
            self.assertEqual(proxy.result_keys, [final["idempotency_key"]])
            self.assertEqual(
                _mysql_scalar(
                    "SELECT status FROM group_buy_order_list "
                    "WHERE out_trade_no='930000000001'"
                ),
                "2",
            )
            self.assertEqual(
                _mysql_scalar("SELECT COUNT(*) FROM notify_task"),
                "1",
            )
        finally:
            if proxy is not None:
                proxy.close()
            self._restart_api(REAL_JAVA_BASE_URL, refund_timeout=5.0)

    def test_10_kill_before_java_receives_refund_abandons(self) -> None:
        proxy: _RefundRecordingProxy | None = None
        try:
            final, summary, proxy = self._kill_and_reconcile(
                delay_mode="before_forward_hold",
            )
            time.sleep(0.5)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(final["status"], "FAILED")
            self.assertEqual(
                final["result_code"],
                "NOT_RECEIVED_BEFORE_QUERY",
            )
            self.assertEqual(final["refund_executed"], 0)
            self.assertEqual(len(proxy.refund_keys), 1)
            self.assertEqual(proxy.result_keys, [final["idempotency_key"]])
            self.assertEqual(
                _mysql_scalar(
                    "SELECT status FROM group_buy_order_list "
                    "WHERE out_trade_no='930000000001'"
                ),
                "0",
            )
            self.assertEqual(
                _mysql_scalar("SELECT COUNT(*) FROM notify_task"),
                "0",
            )
        finally:
            if proxy is not None:
                proxy.close()
            self._restart_api(REAL_JAVA_BASE_URL, refund_timeout=5.0)

    def test_11_java_unavailable_then_result_reconcile_after_recovery(
        self,
    ) -> None:
        proxy = _RefundRecordingProxy(
            REAL_JAVA_BASE_URL,
            refund_delay_mode="service_unavailable",
        )
        proxy.start()
        try:
            self._restart_api(
                f"http://127.0.0.1:{proxy.port}",
                refund_timeout=1.0,
            )
            event, action, _ = self._proposal(
                "agent_c3_unpaid",
                "930000000001",
            )
            response = self._confirm(event, "agent_c3_unpaid")
            unknown = self.store.get_action(str(action["action_id"]))
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(unknown["status"], "UNKNOWN")
            self.assertEqual(proxy.refund_keys, [action["idempotency_key"]])

            environment = {
                "AGENT_ACTION_DB_PATH": str(self.action_path),
                "JAVA_BASE_URL": REAL_JAVA_BASE_URL,
                "JAVA_REFUND_RESULT_TIMEOUT_SECONDS": "5",
            }
            with patch.dict(os.environ, environment):
                summary = reconcile_stuck_actions(
                    now=datetime.now(timezone.utc),
                    min_age=0,
                )

            final = self.store.get_action(str(action["action_id"]))
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(final["status"], "FAILED")
            self.assertEqual(
                final["result_code"],
                "NOT_RECEIVED_BEFORE_QUERY",
            )
            self.assertEqual(final["refund_executed"], 0)
            self.assertEqual(len(proxy.refund_keys), 1)
            self.assertEqual(
                _mysql_scalar(
                    "SELECT status FROM group_buy_order_list "
                    "WHERE out_trade_no='930000000001'"
                ),
                "0",
            )
            self.assertEqual(
                _mysql_scalar("SELECT COUNT(*) FROM notify_task"),
                "0",
            )
        finally:
            proxy.close()
            self._restart_api(REAL_JAVA_BASE_URL, refund_timeout=5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
