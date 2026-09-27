from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.getenv("RUN_B2_INTEGRATION") == "1"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"服务启动失败，退出码 {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"等待端口 {port} 超时")


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def collect_sse(
    url: str,
    payload: dict[str, str],
    *,
    token: str,
    timeout: float = 40,
) -> tuple[list[dict[str, Any]], float]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: str | None = None
    started_at = time.monotonic()
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        with client.stream(
            "POST",
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("event: "):
                    current_event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    current_data = line.removeprefix("data: ")
                elif line == "" and current_event and current_data:
                    events.append(
                        {
                            "event": current_event,
                            "data": json.loads(current_data),
                            "elapsed": time.monotonic() - started_at,
                        }
                    )
                    current_event = None
                    current_data = None
    return events, time.monotonic() - started_at


@unittest.skipUnless(
    RUN_INTEGRATION,
    "设置 RUN_B2_INTEGRATION=1 后运行真实模型 B2 集成测试",
)
class B2IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-b2-",
            dir=PROJECT_ROOT,
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.fake_port = free_port()
        cls.api_port = free_port()
        cls.fake_process: subprocess.Popen[bytes] | None = None
        cls.api_process: subprocess.Popen[bytes] | None = None
        cls.api_log_path = cls.temp_path / "api.log"
        cls.api_log = cls.api_log_path.open("w+b")
        cls.jwt_secret = secrets.token_urlsafe(32)
        cls.token = jwt.encode(
            {"sub": "demo-user", "exp": int(time.time()) + 3600},
            cls.jwt_secret,
            algorithm="HS256",
        )
        cls._start_fake_java(delay_seconds=0)

        launcher = (
            "import os,uvicorn; import api; "
            f"os.environ['FAKE_JAVA_BASE_URL']='http://127.0.0.1:{cls.fake_port}'; "
            f"os.environ['CHECKPOINT_DB_PATH']={str(cls.temp_path / 'checkpoints.sqlite')!r}; "
            "os.environ['AGENT_REQUEST_TIMEOUT_SECONDS']='25'; "
            f"os.environ['JWT_SECRET']={cls.jwt_secret!r}; "
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
        cls.url = f"http://127.0.0.1:{cls.api_port}/api/v1/chat/stream"

    @classmethod
    def tearDownClass(cls) -> None:
        stop_process(cls.api_process)
        stop_process(cls.fake_process)
        cls.api_log.close()
        cls.temporary_directory.cleanup()

    @classmethod
    def _start_fake_java(cls, delay_seconds: float) -> None:
        stop_process(cls.fake_process)
        environment = os.environ.copy()
        environment["FAKE_JAVA_DELAY_SECONDS"] = str(delay_seconds)
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

    def test_1_sse_streaming(self) -> None:
        events, elapsed = collect_sse(
            self.url,
            {
                "session_id": f"sse-{uuid.uuid4().hex}",
                "message": "活动 100123 还有效吗",
            },
            token=self.token,
        )
        self.assertEqual(events[0]["event"], "progress")
        self.assertEqual(events[0]["data"]["stage"], "understanding")
        self.assertTrue(
            any(
                item["event"] == "progress"
                and item["data"].get("stage") == "tool"
                for item in events
            )
        )
        self.assertEqual(events[-1]["event"], "final")
        self.assertEqual(events[-1]["data"]["kind"], "ANSWER")
        self.assertLess(events[0]["elapsed"], events[-1]["elapsed"])
        self.assertLess(events[0]["elapsed"], 2)
        self.assertGreater(elapsed, events[0]["elapsed"])

    def test_2_session_persistence_over_http(self) -> None:
        session_id = f"http-session-{uuid.uuid4().hex}"
        first, _ = collect_sse(
            self.url,
            {"session_id": session_id, "message": "这个活动还有效吗"},
            token=self.token,
        )
        self.assertEqual(first[-1]["data"]["kind"], "REQUEST_INPUT")

        second, _ = collect_sse(
            self.url,
            {"session_id": session_id, "message": "100123"},
            token=self.token,
        )
        self.assertEqual(second[-1]["data"]["kind"], "ANSWER")
        self.assertTrue(
            any(
                item["data"].get("tool") == "get_activity_facts"
                for item in second
            )
        )

    def test_3_client_disconnect(self) -> None:
        session_id = f"disconnect-{uuid.uuid4().hex}"
        with httpx.Client(timeout=10, trust_env=False) as client:
            with client.stream(
                "POST",
                self.url,
                json={
                    "session_id": session_id,
                    "message": "为什么我不能参加活动 100123",
                },
                headers={"Authorization": f"Bearer {self.token}"},
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line == "":
                        break

        deadline = time.monotonic() + 5
        log_text = ""
        while time.monotonic() < deadline:
            time.sleep(0.1)
            log_text = self.api_log_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            if (
                f"client_disconnected session_id={session_id}" in log_text
                and f"cancelled session_id={session_id}" in log_text
            ):
                break
        self.assertIn(f"client_disconnected session_id={session_id}", log_text)
        self.assertIn(f"cancelled session_id={session_id}", log_text)

    def test_4_request_timeout(self) -> None:
        self._start_fake_java(delay_seconds=30)
        events, elapsed = collect_sse(
            self.url,
            {
                "session_id": f"timeout-{uuid.uuid4().hex}",
                "message": "活动 100123 还有效吗",
            },
            token=self.token,
            timeout=35,
        )
        self.assertEqual(events[-1]["event"], "timeout")
        self.assertEqual(events[-1]["data"]["kind"], "HANDOFF")
        self.assertGreaterEqual(elapsed, 24)
        self.assertLess(elapsed, 28)


if __name__ == "__main__":
    unittest.main()
