from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx
from pydantic import ValidationError

from tools.facts import FactsEnvelope, RefundResultFacts
from tools.facts_client import _log_request, _request_target
from tools.refund_execute import (
    ExecutionCertainty,
    RefundExecutionResult,
)


REFUND_RESULT_HTTP_CLIENT = httpx.Client(trust_env=False)
REFUND_RESULT_PATH = "/api/v1/agent/order/refund/result"
logger = logging.getLogger("group_buy_agent.refund_result")


def _result_timeout() -> httpx.Timeout:
    configured = os.getenv(
        "JAVA_REFUND_RESULT_TIMEOUT_SECONDS",
        os.getenv("JAVA_REFUND_TIMEOUT_SECONDS", "5"),
    )
    try:
        seconds = float(configured)
    except ValueError:
        seconds = 5.0
    seconds = max(seconds, 0.001)
    return httpx.Timeout(seconds, connect=min(seconds, 2.0))


def _unknown(result_code: str) -> RefundExecutionResult:
    return RefundExecutionResult(
        certainty=ExecutionCertainty.UNKNOWN,
        result_code=result_code,
    )


def query_refund_result(
    action: dict[str, Any],
    *,
    request_id: str = "",
) -> RefundExecutionResult:
    effective_request_id = request_id or f"refund-result-{uuid.uuid4().hex}"
    started_at = time.perf_counter()
    mode = "real" if os.getenv("JAVA_BASE_URL", "").strip() else "fake"
    try:
        base_url, headers, mode = _request_target(
            user_id=str(action["user_id"]),
            request_id=effective_request_id,
        )
        body = {"idempotencyKey": str(action["idempotency_key"])}
    except (KeyError, TypeError, RuntimeError):
        _log_request(
            path=REFUND_RESULT_PATH,
            started_at=started_at,
            response_code="CONFIG_ERROR",
            request_id=effective_request_id,
            mode=mode,
        )
        return _unknown("CLIENT_CONFIGURATION_ERROR")

    try:
        response = REFUND_RESULT_HTTP_CLIENT.post(
            f"{base_url.rstrip('/')}{REFUND_RESULT_PATH}",
            json=body,
            headers=headers,
            timeout=_result_timeout(),
        )
    except httpx.TimeoutException:
        _log_request(
            path=REFUND_RESULT_PATH,
            started_at=started_at,
            response_code="TIMEOUT",
            request_id=effective_request_id,
            mode=mode,
        )
        return _unknown("RESULT_QUERY_TIMEOUT")
    except (httpx.ConnectError, httpx.RequestError):
        _log_request(
            path=REFUND_RESULT_PATH,
            started_at=started_at,
            response_code="CONNECTION_ERROR",
            request_id=effective_request_id,
            mode=mode,
        )
        return _unknown("RESULT_QUERY_CONNECTION_ERROR")

    _log_request(
        path=REFUND_RESULT_PATH,
        started_at=started_at,
        response_code=response.status_code,
        request_id=effective_request_id,
        mode=mode,
    )
    if response.status_code >= 500:
        return _unknown("RESULT_QUERY_HTTP_5XX")
    if not 200 <= response.status_code < 300:
        return _unknown("RESULT_QUERY_HTTP_ERROR")

    try:
        envelope = FactsEnvelope.model_validate(response.json())
    except (ValueError, ValidationError):
        return _unknown("INVALID_JAVA_RESPONSE")

    if envelope.code != "0000":
        result_code = str(envelope.code or "UNKNOWN")
        if "NOT_FOUND" in result_code:
            logger.error(
                "refund_result_not_found request_id=%s mode=%s",
                effective_request_id,
                mode,
            )
            return _unknown("NOT_FOUND")
        return _unknown(f"JAVA_{result_code}")

    try:
        facts = RefundResultFacts.model_validate(envelope.data)
    except ValidationError:
        return _unknown("INVALID_JAVA_RESPONSE")

    certainty = {
        "SUCCEEDED": ExecutionCertainty.SUCCESS,
        "FAILED": ExecutionCertainty.FAILURE,
        "ABANDONED": ExecutionCertainty.FAILURE,
        "PROCESSING": ExecutionCertainty.UNKNOWN,
    }[facts.status]
    return RefundExecutionResult(
        certainty=certainty,
        status=facts.status,
        result_code=facts.result_code,
        refund_executed=facts.refund_executed,
    )
