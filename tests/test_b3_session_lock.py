from __future__ import annotations

import asyncio
import os
import secrets
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

from tests.test_b2 import free_port, stop_process, wait_for_port


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.getenv("RUN_B3_SESSION_LOCK_INTEGRATION") == "1"


@unittest.skipUnless(
    RUN_INTEGRATION,
    "设置 RUN_B3_SESSION_LOCK_INTEGRATION=1 后运行 B3-2 并发测试",
)
class B3SessionLockIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-b3-lock-", dir=PROJECT_ROOT
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.fake_port = free_port()
        cls.api_port = free_port()
        cls.jwt_secret = secrets.token_urlsafe(32)
        cls.token = jwt.encode(
            {"sub": "demo-user", "exp": int(time.time()) + 3600},
            cls.jwt_secret,
            algorithm="HS256",
        )
        cls.fake_process: subprocess.Popen[bytes] | None = None
        cls.api_process: subprocess.Popen[bytes] | None = None
        cls.api_log = (cls.temp_path / "api.log").open("w+b")

        fake_environment = os.environ.copy()
        fake_environment["FAKE_JAVA_DELAY_SECONDS"] = "3"
        cls.fake_process = subprocess.Popen(
            [
                sys.executable, "-B", "-m", "uvicorn", "fake_java.main:app",
                "--host", "127.0.0.1", "--port", str(cls.fake_port),
                "--log-level", "error",
            ],
            cwd=PROJECT_ROOT,
            env=fake_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_port(cls.fake_process, cls.fake_port)

        checkpoint_path = cls.temp_path / "checkpoints.sqlite"
        launcher = (
            "import os,uvicorn; import api; "
            f"os.environ['FAKE_JAVA_BASE_URL']='http://127.0.0.1:{cls.fake_port}'; "
            f"os.environ['CHECKPOINT_DB_PATH']={str(checkpoint_path)!r}; "
            f"os.environ['JWT_SECRET']={cls.jwt_secret!r}; "
            "os.environ['AGENT_REQUEST_TIMEOUT_SECONDS']='25'; "
            f"uvicorn.run(api.app,host='127.0.0.1',port={cls.api_port},log_level='info')"
        )
        api_environment = os.environ.copy()
        api_environment["PYTHONIOENCODING"] = "utf-8"
        cls.api_process = subprocess.Popen(
            [sys.executable, "-B", "-c", launcher],
            cwd=PROJECT_ROOT,
            env=api_environment,
            stdout=cls.api_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_port(cls.api_process, cls.api_port)
        cls.url = f"http://127.0.0.1:{cls.api_port}/api/v1/chat/stream"
        cls.headers = {"Authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls) -> None:
        stop_process(cls.api_process)
        stop_process(cls.fake_process)
        cls.api_log.close()
        cls.temporary_directory.cleanup()

    def test_1_twenty_same_session_requests_are_fail_fast(self) -> None:
        async def scenario() -> tuple[list[httpx.Response], httpx.Response]:
            session_id = f"busy-{uuid.uuid4().hex}"
            payload = {
                "session_id": session_id,
                "message": "活动 100123 还有效吗",
            }
            async with httpx.AsyncClient(
                timeout=40,
                trust_env=False,
                limits=httpx.Limits(max_connections=25),
            ) as client:
                responses = await asyncio.gather(
                    *(client.post(self.url, json=payload, headers=self.headers) for _ in range(20))
                )
                follow_up = await client.post(
                    self.url,
                    json={"session_id": session_id, "message": "活动 100124 还有效吗"},
                    headers=self.headers,
                )
            return responses, follow_up

        responses, follow_up = asyncio.run(scenario())
        entered = [item for item in responses if item.status_code == 200]
        busy = [item for item in responses if item.status_code == 409]
        self.assertEqual(len(entered), 1)
        self.assertEqual(len(busy), 19)
        self.assertTrue(all(item.json()["detail"]["code"] == "SESSION_BUSY" for item in busy))
        self.assertIn("event: final", entered[0].text)
        self.assertEqual(follow_up.status_code, 200)
        self.assertIn("event: final", follow_up.text)

    def test_2_different_sessions_start_concurrently(self) -> None:
        async def request(
            client: httpx.AsyncClient, session_id: str, started_at: float
        ) -> dict[str, Any]:
            first_event_at: float | None = None
            async with client.stream(
                "POST",
                self.url,
                json={"session_id": session_id, "message": "活动 100123 还有效吗"},
                headers=self.headers,
            ) as response:
                lines: list[str] = []
                async for line in response.aiter_lines():
                    lines.append(line)
                    if line == "" and first_event_at is None:
                        first_event_at = time.monotonic() - started_at
                return {
                    "status": response.status_code,
                    "first_event_at": first_event_at,
                    "body": "\n".join(lines),
                }

        async def scenario() -> list[dict[str, Any]]:
            started_at = time.monotonic()
            async with httpx.AsyncClient(
                timeout=40,
                trust_env=False,
                limits=httpx.Limits(max_connections=4),
            ) as client:
                return await asyncio.gather(
                    request(client, f"parallel-a-{uuid.uuid4().hex}", started_at),
                    request(client, f"parallel-b-{uuid.uuid4().hex}", started_at),
                )

        results = asyncio.run(scenario())
        self.assertTrue(all(item["status"] == 200 for item in results))
        self.assertTrue(all(item["first_event_at"] is not None for item in results))
        self.assertTrue(all(item["first_event_at"] < 2 for item in results))
        self.assertTrue(all("event: final" in item["body"] for item in results))


if __name__ == "__main__":
    unittest.main()
