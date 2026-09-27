import os
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from tools.facts import FactsEnvelope, ToolResult


FACTS_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
FACTS_HTTP_CLIENT = httpx.Client(timeout=FACTS_TIMEOUT, trust_env=False)

BUSINESS_RESULT_CODES = {
    "AUTH_REQUIRED",
    "ACTIVITY_NOT_FOUND",
    "ACTIVITY_NOT_FOUND_OR_NOT_AUTHORIZED",
    "NOT_AUTHORIZED",
    "NOT_FOUND",
    "ORDER_NOT_FOUND_OR_NOT_AUTHORIZED",
}


def _result(
    *,
    success: bool,
    code: str,
    message: str,
    retryable: bool,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ToolResult(
        success=success,
        code=code,
        message=message,
        retryable=retryable,
        data=data,
    ).as_dict()


def _service_unavailable() -> dict[str, Any]:
    return _result(
        success=False,
        code="SERVICE_UNAVAILABLE",
        message="业务服务暂时不可用，暂时无法查询",
        retryable=True,
    )


def _invalid_response() -> dict[str, Any]:
    return _result(
        success=False,
        code="INVALID_TOOL_RESPONSE",
        message="业务服务响应不符合契约，暂时无法查询",
        retryable=False,
    )


def query_facts(
    *,
    path: str,
    body: dict[str, object],
    user_id: str,
    data_model: type[BaseModel],
) -> dict[str, Any]:
    base_url = os.getenv("FAKE_JAVA_BASE_URL", "http://127.0.0.1:8000")
    try:
        response = FACTS_HTTP_CLIENT.post(
            f"{base_url.rstrip('/')}{path}",
            json=body,
            headers={"X-Dev-Authenticated-User-Id": user_id},
        )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError):
        return _service_unavailable()

    if response.status_code >= 500:
        return _service_unavailable()
    if not 200 <= response.status_code < 300:
        return _invalid_response()

    try:
        envelope = FactsEnvelope.model_validate(response.json())
    except (ValueError, ValidationError):
        return _invalid_response()

    if envelope.code in BUSINESS_RESULT_CODES:
        return _result(
            success=False,
            code=envelope.code,
            message=envelope.info,
            retryable=False,
        )
    if envelope.code != "0000":
        return _invalid_response()

    try:
        facts = data_model.model_validate(envelope.data)
    except ValidationError:
        return _invalid_response()

    return _result(
        success=True,
        code="0000",
        message=envelope.info,
        retryable=False,
        data=facts.model_dump(mode="json"),
    )
