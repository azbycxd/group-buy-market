from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx
import jwt

from tests.test_b2 import collect_sse, free_port, stop_process, wait_for_port


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.getenv("RUN_B3_INTEGRATION") == "1"


@unittest.skipUnless(RUN_INTEGRATION, "设置 RUN_B3_INTEGRATION=1 后运行 B3-1 集成测试")
class B3AuthenticationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-b3-", dir=PROJECT_ROOT
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.checkpoint_path = cls.temp_path / "checkpoints.sqlite"
        cls.fake_port = free_port()
        cls.api_port = free_port()
        cls.jwt_secret = secrets.token_urlsafe(32)
        cls.user_a_token = cls._token("user-A")
        cls.user_b_token = cls._token("user-B")
        cls.user_002_token = cls._token("user-002")
        cls.fake_process: subprocess.Popen[bytes] | None = None
        cls.api_process: subprocess.Popen[bytes] | None = None
        cls.api_log = (cls.temp_path / "api.log").open("w+b")

        environment = os.environ.copy()
        environment["FAKE_JAVA_DELAY_SECONDS"] = "0"
        cls.fake_process = subprocess.Popen(
            [
                sys.executable, "-B", "-m", "uvicorn", "fake_java.main:app",
                "--host", "127.0.0.1", "--port", str(cls.fake_port),
                "--log-level", "error",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wait_for_port(cls.fake_process, cls.fake_port)
        cls._start_api()

    @classmethod
    def tearDownClass(cls) -> None:
        stop_process(cls.api_process)
        stop_process(cls.fake_process)
        cls.api_log.close()
        cls.temporary_directory.cleanup()

    @classmethod
    def _token(cls, user_id: str, expires_in: int = 3600) -> str:
        return jwt.encode(
            {"sub": user_id, "exp": int(time.time()) + expires_in},
            cls.jwt_secret,
            algorithm="HS256",
        )

    @classmethod
    def _start_api(cls) -> None:
        stop_process(cls.api_process)
        launcher = (
            "import os,uvicorn; import api; "
            f"os.environ['FAKE_JAVA_BASE_URL']='http://127.0.0.1:{cls.fake_port}'; "
            f"os.environ['CHECKPOINT_DB_PATH']={str(cls.checkpoint_path)!r}; "
            f"os.environ['JWT_SECRET']={cls.jwt_secret!r}; "
            f"uvicorn.run(api.app,host='127.0.0.1',port={cls.api_port},log_level='info')"
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

    def test_1_invalid_tokens_return_401(self) -> None:
        payload = {"session_id": f"auth-{uuid.uuid4().hex}", "message": "活动100123还有效吗"}
        forged = jwt.encode(
            {"sub": "user-A", "exp": int(time.time()) + 3600},
            "wrong-secret",
            algorithm="HS256",
        )
        expired = self._token("user-A", -1)
        with httpx.Client(timeout=5, trust_env=False) as client:
            responses = [
                client.post(self.url, json=payload),
                client.post(self.url, json=payload, headers={"Authorization": f"Bearer {forged}"}),
                client.post(self.url, json=payload, headers={"Authorization": f"Bearer {expired}"}),
            ]
        self.assertTrue(all(item.status_code == 401 for item in responses))

    def test_2_ownership_and_restart(self) -> None:
        session_id = f"owned-{uuid.uuid4().hex}"
        first, _ = collect_sse(
            self.url,
            {"session_id": session_id, "message": "这个活动还有效吗"},
            token=self.user_a_token,
        )
        self.assertEqual(first[-1]["data"]["kind"], "REQUEST_INPUT")
        with httpx.Client(timeout=5, trust_env=False) as client:
            forbidden = client.post(
                self.url,
                json={"session_id": session_id, "message": "100123"},
                headers={"Authorization": f"Bearer {self.user_b_token}"},
            )
        self.assertEqual(forbidden.status_code, 403)
        self._start_api()
        with httpx.Client(timeout=5, trust_env=False) as client:
            forbidden_after_restart = client.post(
                self.url,
                json={"session_id": session_id, "message": "100123"},
                headers={"Authorization": f"Bearer {self.user_b_token}"},
            )
        self.assertEqual(forbidden_after_restart.status_code, 403)

    def test_3_authenticated_multiturn_and_runtime_identity(self) -> None:
        session_id = f"multiturn-{uuid.uuid4().hex}"
        first, _ = collect_sse(
            self.url,
            {"session_id": session_id, "message": "这个活动还有效吗"},
            token=self.user_a_token,
        )
        second, _ = collect_sse(
            self.url,
            {"session_id": session_id, "message": "100123"},
            token=self.user_a_token,
        )
        self.assertEqual(first[-1]["data"]["kind"], "REQUEST_INPUT")
        self.assertEqual(second[-1]["data"]["kind"], "ANSWER")
        order, _ = collect_sse(
            self.url,
            {"session_id": f"runtime-{uuid.uuid4().hex}", "message": "订单 ORD100002 现在什么状态"},
            token=self.user_002_token,
        )
        self.assertIn("NORMAL", order[-1]["data"]["answer"])


if __name__ == "__main__":
    unittest.main()
