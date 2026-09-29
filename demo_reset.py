from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from demo import DEMO_USER_ID
from tools.facts_client import _internal_service_jwt


logger = logging.getLogger("group_buy_agent.demo_reset")
DEMO_RESET_PATH = "/api/v1/agent/demo/reset"


class DemoResetStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DemoResetResult:
    status: DemoResetStatus
    code: str


def reset_demo_java(*, request_id: str) -> DemoResetResult:
    base_url = os.getenv("JAVA_BASE_URL", "").strip()
    if not base_url:
        return DemoResetResult(
            DemoResetStatus.UNAVAILABLE, "JAVA_BASE_URL_MISSING"
        )
    started_at = time.perf_counter()
    response_code: str | int = "NO_RESPONSE"
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}{DEMO_RESET_PATH}",
            headers={
                "Authorization": (
                    f"Bearer {_internal_service_jwt(DEMO_USER_ID)}"
                ),
                "X-Request-Id": request_id,
            },
            json={},
            timeout=httpx.Timeout(5.0, connect=2.0),
            trust_env=False,
        )
        response_code = response.status_code
        if response.status_code >= 500:
            return DemoResetResult(DemoResetStatus.UNAVAILABLE, "HTTP_5XX")
        if not 200 <= response.status_code < 300:
            return DemoResetResult(DemoResetStatus.UNAVAILABLE, "HTTP_ERROR")
        payload = response.json()
        code = str(payload.get("code") or "INVALID_RESPONSE")
        if code == "0000":
            return DemoResetResult(DemoResetStatus.SUCCESS, code)
        if code == "DEMO_RESET_BLOCKED":
            return DemoResetResult(DemoResetStatus.BLOCKED, code)
        return DemoResetResult(DemoResetStatus.UNAVAILABLE, code)
    except (httpx.HTTPError, ValueError, TypeError):
        return DemoResetResult(DemoResetStatus.UNAVAILABLE, "REQUEST_FAILED")
    finally:
        logger.info(
            "demo_reset_request path=%s latency_ms=%.1f response_code=%s "
            "request_id=%s",
            DEMO_RESET_PATH,
            (time.perf_counter() - started_at) * 1000,
            response_code,
            request_id,
        )
