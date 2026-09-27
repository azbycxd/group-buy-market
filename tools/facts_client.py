import logging
import os
import time
import uuid
from typing import Any

import httpx
import jwt
from pydantic import BaseModel, ValidationError

from tools.facts import FactsEnvelope, ToolResult


FACTS_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
FACTS_HTTP_CLIENT = httpx.Client(timeout=FACTS_TIMEOUT, trust_env=False)
INTERNAL_JWT_ALGORITHM = "HS256"
INTERNAL_JWT_TTL_SECONDS = 60
logger = logging.getLogger("group_buy_agent.facts_client")

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


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment: {name}")
    return value


def _internal_service_jwt(user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "iss": _required_environment("JAVA_INTERNAL_JWT_ISSUER"),
            "aud": _required_environment("JAVA_INTERNAL_JWT_AUDIENCE"),
            "iat": now,
            "exp": now + INTERNAL_JWT_TTL_SECONDS,
        },
        _required_environment("JAVA_INTERNAL_JWT_SECRET"),
        algorithm=INTERNAL_JWT_ALGORITHM,
    )


def _request_target(
    *,
    user_id: str,
    request_id: str,
) -> tuple[str, dict[str, str], str]:
    real_base_url = os.getenv("JAVA_BASE_URL", "").strip()
    if real_base_url:
        return (
            real_base_url,
            {
                "Authorization": f"Bearer {_internal_service_jwt(user_id)}",
                "X-Request-Id": request_id,
            },
            "real",
        )
    return (
        os.getenv("FAKE_JAVA_BASE_URL", "http://127.0.0.1:8000"),
        {
            "X-Dev-Authenticated-User-Id": user_id,
            "X-Request-Id": request_id,
        },
        "fake",
    )


def _log_request(
    *,
    path: str,
    started_at: float,
    response_code: str | int,
    request_id: str,
    mode: str,
) -> None:
    logger.info(
        "facts_request path=%s latency_ms=%.1f response_code=%s "
        "request_id=%s mode=%s",
        path,
        (time.perf_counter() - started_at) * 1000,
        response_code,
        request_id,
        mode,
    )


def query_facts(
    *,
    path: str,
    body: dict[str, object],
    user_id: str,
    request_id: str = "",
    data_model: type[BaseModel],
) -> dict[str, Any]:
    effective_request_id = request_id or f"tool-{uuid.uuid4().hex}"
    started_at = time.perf_counter()
    mode = "real" if os.getenv("JAVA_BASE_URL", "").strip() else "fake"
    try:
        base_url, headers, mode = _request_target(
            user_id=user_id,
            request_id=effective_request_id,
        )
    except RuntimeError:
        _log_request(
            path=path,
            started_at=started_at,
            response_code="CONFIG_ERROR",
            request_id=effective_request_id,
            mode=mode,
        )
        return _service_unavailable()

    try:
        response = FACTS_HTTP_CLIENT.post(
            f"{base_url.rstrip('/')}{path}",
            json=body,
            headers=headers,
        )
    except httpx.TimeoutException:
        _log_request(
            path=path,
            started_at=started_at,
            response_code="TIMEOUT",
            request_id=effective_request_id,
            mode=mode,
        )
        return _service_unavailable()
    except (httpx.ConnectError, httpx.RequestError):
        _log_request(
            path=path,
            started_at=started_at,
            response_code="CONNECTION_ERROR",
            request_id=effective_request_id,
            mode=mode,
        )
        return _service_unavailable()

    _log_request(
        path=path,
        started_at=started_at,
        response_code=response.status_code,
        request_id=effective_request_id,
        mode=mode,
    )

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
