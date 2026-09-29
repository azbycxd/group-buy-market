from __future__ import annotations

import asyncio
import ipaddress
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import jwt
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


DEMO_USER_ID = "demo_user"
DEMO_SESSION_TTL_SECONDS = 30 * 60
DEMO_IDLE_TIMEOUT_SECONDS = 5 * 60
DEMO_RATE_LIMIT = 20
DEMO_RATE_WINDOW_SECONDS = 60.0
DEMO_SESSION_IP_LIMIT = 10
DEMO_SESSION_IP_WINDOW_SECONDS = 60 * 60
DEMO_DAILY_TOKEN_LIMIT = 500_000
DEMO_QUOTA_EXHAUSTED_MESSAGE = "今日演示额度已用完，请明天再试。"
DEMO_ORDERS = {
    "unpaid": "930000000001",
    "paid_unformed": "930000000002",
    "paid_formed": "930000000003",
    "closed": "930000000004",
}


class DemoSessionBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoLeaseSnapshot:
    lease_id: str
    lease_token: str
    created_at: float
    expires_at: float
    last_activity: float


@dataclass(frozen=True)
class DemoTokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class DemoTokenCounter(BaseCallbackHandler):
    """Uses the same response metadata paths as the final E1 evaluation."""

    def __init__(
        self,
        *,
        recorder: Callable[[int | None, int | None, int], object] | None = None,
    ) -> None:
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._saw_usage = False
        self._breakdown_complete = True
        self._lock = threading.Lock()
        self._recorder = recorder

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        del kwargs
        handled = False
        for generation_list in response.generations:
            for generation in generation_list:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if isinstance(usage, dict):
                    handled = self._record_usage(usage) or handled

        if not handled and isinstance(response.llm_output, dict):
            usage = response.llm_output.get(
                "token_usage"
            ) or response.llm_output.get("usage")
            if isinstance(usage, dict):
                self._record_usage(usage)

    def _record_usage(self, usage: dict[str, Any]) -> bool:
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get(
            "output_tokens", usage.get("completion_tokens")
        )
        total_tokens = usage.get("total_tokens")
        if (
            not isinstance(total_tokens, int)
            and isinstance(input_tokens, int)
            and isinstance(output_tokens, int)
        ):
            total_tokens = input_tokens + output_tokens
        if not isinstance(total_tokens, int):
            return False

        with self._lock:
            self._saw_usage = True
            self._total_tokens += total_tokens
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                self._input_tokens += input_tokens
                self._output_tokens += output_tokens
            else:
                self._breakdown_complete = False
        if self._recorder is not None:
            self._recorder(input_tokens, output_tokens, total_tokens)
        return True

    def usage(self) -> DemoTokenUsage:
        with self._lock:
            if not self._saw_usage:
                return DemoTokenUsage(None, None, None)
            return DemoTokenUsage(
                input_tokens=(
                    self._input_tokens if self._breakdown_complete else None
                ),
                output_tokens=(
                    self._output_tokens if self._breakdown_complete else None
                ),
                total_tokens=self._total_tokens,
            )


class DemoSessionLease:
    """Single-process lease with absolute and idle expiry."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEMO_SESSION_TTL_SECONDS,
        idle_timeout_seconds: int = DEMO_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._lease_id: str | None = None
        self._lease_token: str | None = None
        self._created_at = 0.0
        self._expires_at = 0.0
        self._last_activity = 0.0

    def _snapshot_unlocked(self) -> DemoLeaseSnapshot | None:
        if self._lease_id is None or self._lease_token is None:
            return None
        return DemoLeaseSnapshot(
            lease_id=self._lease_id,
            lease_token=self._lease_token,
            created_at=self._created_at,
            expires_at=self._expires_at,
            last_activity=self._last_activity,
        )

    async def snapshot(self) -> DemoLeaseSnapshot | None:
        async with self._lock:
            return self._snapshot_unlocked()

    def _is_current_unlocked(self, now: float) -> bool:
        return (
            self._lease_id is not None
            and now < self._expires_at
            and now - self._last_activity < self._idle_timeout_seconds
        )

    async def is_current(self) -> bool:
        async with self._lock:
            return self._is_current_unlocked(self._clock())

    async def issue(
        self,
        *,
        jwt_secret: str,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        """Create or recover an active lease; stale leases require reset first."""
        async with self._lock:
            now = self._clock()
            if self._lease_id is not None:
                if not self._is_current_unlocked(now):
                    raise DemoSessionBusyError("stale lease requires reset")
                if (
                    not lease_token
                    or self._lease_token is None
                    or not secrets.compare_digest(lease_token, self._lease_token)
                ):
                    raise DemoSessionBusyError
                self._last_activity = now
                return self._grant(jwt_secret=jwt_secret, now=now)

            self._lease_id = uuid.uuid4().hex
            self._lease_token = secrets.token_urlsafe(32)
            self._created_at = now
            self._expires_at = now + self._ttl_seconds
            self._last_activity = now
            return self._grant(jwt_secret=jwt_secret, now=now)

    async def clear(self, *, expected_lease_id: str) -> bool:
        async with self._lock:
            if self._lease_id != expected_lease_id:
                return False
            self._lease_id = None
            self._lease_token = None
            self._created_at = 0.0
            self._expires_at = 0.0
            self._last_activity = 0.0
            return True

    async def touch(self, *, lease_id: str) -> bool:
        async with self._lock:
            now = self._clock()
            if self._lease_id != lease_id or not self._is_current_unlocked(now):
                return False
            self._last_activity = now
            return True

    def _grant(self, *, jwt_secret: str, now: float) -> dict[str, Any]:
        if self._lease_id is None or self._lease_token is None:
            raise RuntimeError("Demo lease 尚未建立")
        token = jwt.encode(
            {
                "sub": DEMO_USER_ID,
                "iat": int(now),
                "exp": int(self._expires_at),
                "jti": uuid.uuid4().hex,
                "demo_lease_id": self._lease_id,
            },
            jwt_secret,
            algorithm="HS256",
        )
        return {
            "token": token,
            "lease_token": self._lease_token,
            "expires_at": datetime.fromtimestamp(
                self._expires_at, tz=timezone.utc
            ).isoformat(),
            "demo_orders": dict(DEMO_ORDERS),
        }

    def accepts(self, claims: object) -> bool:
        if not isinstance(claims, dict):
            return False
        return (
            claims.get("sub") == DEMO_USER_ID
            and claims.get("demo_lease_id") == self._lease_id
            and self._is_current_unlocked(self._clock())
        )


class DemoRateLimiter:
    """Small in-memory sliding-window limiter for Demo chat requests."""

    def __init__(
        self,
        *,
        limit: int = DEMO_RATE_LIMIT,
        window_seconds: float = DEMO_RATE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        async with self._lock:
            now = self._clock()
            requests = self._requests[key]
            cutoff = now - self._window_seconds
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self._limit:
                return False
            requests.append(now)
            return True


def trusted_client_ip(
    *,
    direct_ip: str | None,
    forwarded_for: str | None,
    trust_proxy_headers: bool,
    trusted_proxy_ips: set[str],
) -> str:
    direct = (direct_ip or "unknown").strip() or "unknown"
    if not trust_proxy_headers or direct not in trusted_proxy_ips:
        return direct
    candidate = (forwarded_for or "").split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return direct
