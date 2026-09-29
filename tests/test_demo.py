from __future__ import annotations

import asyncio
import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt

from api import create_app
from demo import (
    DEMO_ORDERS,
    DEMO_USER_ID,
    DemoRateLimiter,
    DemoSessionBusyError,
    DemoSessionLease,
)
from demo_store import DemoPersistentStore


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class DemoLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_identity_contract_busy_and_expiry(self) -> None:
        clock = MutableClock()
        lease = DemoSessionLease(ttl_seconds=1800, clock=clock)
        secret = "demo-test-secret-at-least-32-characters"

        payload = await lease.issue(jwt_secret=secret)
        claims = jwt.decode(
            payload["token"],
            secret,
            algorithms=["HS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(claims["sub"], DEMO_USER_ID)
        self.assertEqual(claims["exp"] - claims["iat"], 1800)
        self.assertEqual(payload["demo_orders"], DEMO_ORDERS)
        self.assertTrue(lease.accepts(claims))

        recovered = await lease.issue(
            jwt_secret=secret,
            lease_token=payload["lease_token"],
        )
        self.assertEqual(recovered["lease_token"], payload["lease_token"])
        self.assertNotEqual(recovered["token"], payload["token"])

        with self.assertRaises(DemoSessionBusyError):
            await lease.issue(jwt_secret=secret)
        with self.assertRaises(DemoSessionBusyError):
            await lease.issue(
                jwt_secret=secret,
                lease_token="wrong-lease-token-with-enough-characters",
            )

        clock.value += 1801
        with self.assertRaises(DemoSessionBusyError):
            await lease.issue(
                jwt_secret=secret,
                lease_token=payload["lease_token"],
            )
        snapshot = await lease.snapshot()
        self.assertIsNotNone(snapshot)
        await lease.clear(expected_lease_id=snapshot.lease_id)
        replacement = await lease.issue(jwt_secret=secret)
        self.assertNotEqual(replacement["token"], payload["token"])
        self.assertNotEqual(
            replacement["lease_token"],
            payload["lease_token"],
        )
        self.assertFalse(lease.accepts(claims))

    async def test_rate_limit_twenty_requests_per_minute(self) -> None:
        clock = MutableClock()
        limiter = DemoRateLimiter(
            limit=20,
            window_seconds=60,
            clock=clock,
        )
        results = [await limiter.allow("demo-session") for _ in range(21)]
        self.assertEqual(results.count(True), 20)
        self.assertFalse(results[-1])
        clock.value += 61
        self.assertTrue(await limiter.allow("demo-session"))


class DemoRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_route_contract_and_429(self) -> None:
        secret = "route-test-secret-at-least-32-characters"
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DEMO_ENABLED": "true"}, clear=False):
                app = create_app()
            app.state.jwt_secret = secret
            app.state.demo_lease = DemoSessionLease()
            app.state.demo_store = DemoPersistentStore(
                Path(temporary) / "demo.sqlite"
            )
            app.state.demo_lifecycle_lock = asyncio.Lock()
            app.state.demo_trust_proxy_headers = False
            app.state.demo_trusted_proxy_ips = {"127.0.0.1", "::1"}
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                page = await client.get("/")
                first = await client.post("/demo/session")
                recovered = await client.post(
                    "/demo/session",
                    json={"lease_token": first.json()["lease_token"]},
                )
                second = await client.post("/demo/session")
                wrong = await client.post(
                    "/demo/session",
                    json={
                        "lease_token": (
                            "wrong-lease-token-with-enough-characters"
                        )
                    },
                )
                controlled_user = await client.post(
                    "/demo/session",
                    json={"user_id": "attacker-controlled-user"},
                )
                lease_as_bearer = await client.post(
                    "/api/v1/chat/stream",
                    headers={
                        "Authorization": (
                            f"Bearer {first.json()['lease_token']}"
                        )
                    },
                    json={
                        "session_id": "lease-token-is-not-jwt",
                        "message": "查询订单状态",
                    },
                )

        self.assertEqual(page.status_code, 200)
        self.assertNotIn("token", page.text.lower())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            set(first.json()),
            {"token", "lease_token", "expires_at", "demo_orders"},
        )
        claims = jwt.decode(
            first.json()["token"],
            secret,
            algorithms=["HS256"],
        )
        self.assertEqual(claims["sub"], DEMO_USER_ID)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(
            recovered.json()["lease_token"],
            first.json()["lease_token"],
        )
        self.assertNotEqual(recovered.json()["token"], first.json()["token"])
        self.assertEqual(second.status_code, 429)
        self.assertEqual(
            second.json()["detail"],
            "演示环境正在使用，请稍后再试。",
        )
        self.assertEqual(wrong.status_code, 429)
        self.assertEqual(controlled_user.status_code, 422)
        self.assertEqual(lease_as_bearer.status_code, 401)

    async def test_browser_storage_only_persists_lease_token(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "demo_static"
            / "app.js"
        ).read_text(encoding="utf-8")
        storage_writes = [
            line.strip()
            for line in source.splitlines()
            if "Storage.setItem" in line
        ]
        self.assertEqual(
            storage_writes,
            [
                "sessionStorage.setItem("
                "leaseStorageKey, payload.lease_token);"
            ],
        )
        self.assertNotIn("localStorage", source)
        self.assertNotIn("sessionStorage.setItem(leaseStorageKey, token)", source)
        self.assertNotIn(
            "sessionStorage.setItem(leaseStorageKey, pendingAction",
            source,
        )
        self.assertIn("releaseButton.disabled = value;", source)

    async def test_demo_routes_are_absent_when_disabled(self) -> None:
        with patch.dict(os.environ, {"DEMO_ENABLED": "false"}, clear=False):
            app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            page = await client.get("/")
            session = await client.post("/demo/session")
            release = await client.post("/demo/release")
        self.assertEqual(page.status_code, 404)
        self.assertEqual(session.status_code, 404)
        self.assertEqual(release.status_code, 404)


if __name__ == "__main__":
    unittest.main()
