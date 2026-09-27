from __future__ import annotations

import json
import os
import time
import uuid
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from tools.facts import FactsEnvelope, RefundExecutionFacts
from tools.facts_client import _log_request, _request_target


REFUND_HTTP_CLIENT = httpx.Client(trust_env=False)
REFUND_PATH = "/api/v1/agent/order/refund"


class ExecutionCertainty(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNKNOWN = "UNKNOWN"


class RefundExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certainty: ExecutionCertainty
    status: str | None = None
    result_code: str
    refund_executed: bool = False
    idempotent_replay: bool = False


def _execution_timeout() -> httpx.Timeout:
    try:
        seconds = float(os.getenv("JAVA_REFUND_TIMEOUT_SECONDS", "5"))
    except ValueError:
        seconds = 5.0
    seconds = max(seconds, 0.001)
    return httpx.Timeout(seconds, connect=min(seconds, 2.0))


def _unknown(result_code: str) -> RefundExecutionResult:
    return RefundExecutionResult(
        certainty=ExecutionCertainty.UNKNOWN,
        result_code=result_code,
    )


def execute_refund(
    action: dict[str, Any],
    *,
    request_id: str = "",
) -> RefundExecutionResult:
    effective_request_id = request_id or f"refund-{uuid.uuid4().hex}"
    started_at = time.perf_counter()
    mode = "real" if os.getenv("JAVA_BASE_URL", "").strip() else "fake"
    try:
        preview = json.loads(str(action["preview_json"]))
        refund_type = str(preview["refundType"])
        base_url, headers, mode = _request_target(
            user_id=str(action["user_id"]),
            request_id=effective_request_id,
        )
        body = {
            "outTradeNo": str(action["out_trade_no"]),
            "idempotencyKey": str(action["idempotency_key"]),
            "expectedVersion": str(action["expected_version"]),
            "expectedRefundType": refund_type,
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError):
        _log_request(
            path=REFUND_PATH,
            started_at=started_at,
            response_code="CONFIG_ERROR",
            request_id=effective_request_id,
            mode=mode,
        )
        return _unknown("CLIENT_CONFIGURATION_ERROR")

    try:
        response = REFUND_HTTP_CLIENT.post(
            f"{base_url.rstrip('/')}{REFUND_PATH}",
            json=body,
            headers=headers,
            timeout=_execution_timeout(),
        )
    except httpx.TimeoutException:
        _log_request(
            path=REFUND_PATH,
            started_at=started_at,
            response_code="TIMEOUT",
            request_id=effective_request_id,
            mode=mode,
        )
        return _unknown("REQUEST_TIMEOUT")
    except (httpx.ConnectError, httpx.RequestError):
        _log_request(
            path=REFUND_PATH,
            started_at=started_at,
            response_code="CONNECTION_ERROR",
            request_id=effective_request_id,
            mode=mode,
        )
        return _unknown("CONNECTION_ERROR")

    _log_request(
        path=REFUND_PATH,
        started_at=started_at,
        response_code=response.status_code,
        request_id=effective_request_id,
        mode=mode,
    )
    if response.status_code >= 500:
        return _unknown("HTTP_5XX")
    if not 200 <= response.status_code < 300:
        return _unknown("HTTP_ERROR")

    try:
        envelope = FactsEnvelope.model_validate(response.json())
        if envelope.code != "0000":
            return _unknown(f"JAVA_{envelope.code}")
        facts = RefundExecutionFacts.model_validate(envelope.data)
    except (ValueError, ValidationError):
        return _unknown("INVALID_JAVA_RESPONSE")

    certainty = {
        "SUCCEEDED": ExecutionCertainty.SUCCESS,
        "FAILED": ExecutionCertainty.FAILURE,
        "PROCESSING": ExecutionCertainty.UNKNOWN,
    }[facts.status]
    return RefundExecutionResult(
        certainty=certainty,
        status=facts.status,
        result_code=facts.result_code,
        refund_executed=facts.refund_executed,
        idempotent_replay=facts.idempotent_replay,
    )
