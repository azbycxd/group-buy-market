from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import jwt


DEMO_USER_ID = "demo_user"
DEMO_SESSION_TTL_SECONDS = 30 * 60
DEMO_RATE_LIMIT = 20
DEMO_RATE_WINDOW_SECONDS = 60.0
DEMO_ORDERS = {
    "unpaid": "930000000001",
    "paid_unformed": "930000000002",
    "paid_formed": "930000000003",
    "closed": "930000000004",
}


class DemoSessionBusyError(RuntimeError):
    pass


class DemoSessionLease:
    """Single-process lease for the one-visitor interview demo."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEMO_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._lease_id: str | None = None
        self._lease_token: str | None = None
        self._expires_at = 0.0

    async def issue(
        self,
        *,
        jwt_secret: str,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            now = self._clock()
            if self._lease_id is not None and now < self._expires_at:
                if (
                    not lease_token
                    or self._lease_token is None
                    or not secrets.compare_digest(
                        lease_token,
                        self._lease_token,
                    )
                ):
                    raise DemoSessionBusyError
                return self._grant(jwt_secret=jwt_secret, now=now)

            lease_id = uuid.uuid4().hex
            self._lease_token = secrets.token_urlsafe(32)
            expires_at = now + self._ttl_seconds
            self._lease_id = lease_id
            self._expires_at = expires_at
            return self._grant(jwt_secret=jwt_secret, now=now)

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
                self._expires_at,
                tz=timezone.utc,
            ).isoformat(),
            "demo_orders": dict(DEMO_ORDERS),
        }

    def accepts(self, claims: object) -> bool:
        if not isinstance(claims, dict):
            return False
        return (
            claims.get("sub") == DEMO_USER_ID
            and claims.get("demo_lease_id") == self._lease_id
            and self._lease_id is not None
            and self._clock() < self._expires_at
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

    async def allow(self, session_id: str) -> bool:
        async with self._lock:
            now = self._clock()
            requests = self._requests[session_id]
            cutoff = now - self._window_seconds
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self._limit:
                return False
            requests.append(now)
            return True
