from __future__ import annotations

import json
import os
import re
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

from tests.test_b2 import collect_sse, free_port, stop_process


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.getenv("RUN_D3A1_INTEGRATION") == "1"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@unittest.skipUnless(
    RUN_INTEGRATION,
    "设置 RUN_D3A1_INTEGRATION=1 后运行 D3a.1 HTTP warmup 验收",
)
class D3A1HttpWarmupIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="group-buy-agent-d3a1-",
            dir=PROJECT_ROOT,
        )
        cls.temp_path = Path(cls.temporary_directory.name)
        cls.api_port = free_port()
        cls.jwt_secret = secrets.token_urlsafe(32)
        cls.token = jwt.encode(
            {"sub": "d3a1-user", "exp": int(time.time()) + 3600},
            cls.jwt_secret,
            algorithm="HS256",
        )
        cls.api_log_path = cls.temp_path / "api.log"
        cls.api_log = cls.api_log_path.open("w+b")
        launcher = (
            "import os,uvicorn; import api; "
            f"os.environ['CHECKPOINT_DB_PATH']={str(cls.temp_path / 'checkpoints.sqlite')!r}; "
            "os.environ['AGENT_REQUEST_TIMEOUT_SECONDS']='25'; "
            "os.environ['JAVA_BASE_URL']=''; "
            "os.environ['LANGSMITH_TRACING']='false'; "
            f"os.environ['JWT_SECRET']={cls.jwt_secret!r}; "
            f"uvicorn.run(api.app,host='127.0.0.1',port={cls.api_port},log_level='info')"
        )
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        cls.started_at = time.monotonic()
        cls.api_process = subprocess.Popen(
            [sys.executable, "-B", "-c", launcher],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=cls.api_log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cls._wait_until_ready()
        cls.startup_seconds = time.monotonic() - cls.started_at
        cls.log_at_ready = cls._read_log()

    @classmethod
    def tearDownClass(cls) -> None:
        stop_process(cls.api_process)
        cls.api_log.close()
        cls.temporary_directory.cleanup()

    @classmethod
    def _wait_until_ready(cls) -> None:
        deadline = time.monotonic() + 90
        url = f"http://127.0.0.1:{cls.api_port}/docs"
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            while time.monotonic() < deadline:
                if cls.api_process.poll() is not None:
                    raise RuntimeError(
                        f"Agent API 启动失败: {cls._read_log()}"
                    )
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise RuntimeError(f"等待 Agent API ready 超时: {cls._read_log()}")

    @classmethod
    def _read_log(cls) -> str:
        cls.api_log.flush()
        return cls.api_log_path.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def _rule_request(cls) -> tuple[list[dict[str, object]], float]:
        return collect_sse(
            f"http://127.0.0.1:{cls.api_port}/api/v1/chat/stream",
            {
                "session_id": f"d3a1-{uuid.uuid4().hex}",
                "message": "拼团活动的参与次数和资格有什么规则？",
            },
            token=cls.token,
            timeout=35,
        )

    def test_startup_preloads_rag_and_first_request_is_warm(self) -> None:
        match = re.search(
            r"rag_warmup_completed latency_ms=([0-9.]+)",
            self.log_at_ready,
        )
        self.assertIsNotNone(match, self.log_at_ready)
        warmup_ms = float(match.group(1))
        loading_markers_at_ready = self.log_at_ready.count("Loading weights")

        first_events, first_elapsed = self._rule_request()
        self.assertLess(first_elapsed, 25)
        self.assertNotIn("timeout", [item["event"] for item in first_events])
        self.assertEqual(first_events[-1]["event"], "final")
        self.assertEqual(first_events[-1]["data"]["kind"], "ANSWER")

        warm_elapsed: list[float] = []
        for _ in range(5):
            events, elapsed = self._rule_request()
            warm_elapsed.append(elapsed)
            self.assertNotIn("timeout", [item["event"] for item in events])
            self.assertEqual(events[-1]["data"]["kind"], "ANSWER")

        log_after_requests = self._read_log()
        self.assertEqual(
            log_after_requests.count("Loading weights"),
            loading_markers_at_ready,
        )
        summary = {
            "startup_total_ms": self.startup_seconds * 1000,
            "rag_warmup_ms": warmup_ms,
            "first_request_ms": first_elapsed * 1000,
            "warm_5_p50_ms": _percentile(warm_elapsed, 0.50) * 1000,
            "warm_5_p95_ms": _percentile(warm_elapsed, 0.95) * 1000,
            "warm_5_ms": [value * 1000 for value in warm_elapsed],
        }
        print("D3A1_HTTP " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
