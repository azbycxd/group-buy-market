from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import api
from action_ledger import ActionStatus, AgentActionStore
from demo import (
    DEMO_QUOTA_EXHAUSTED_MESSAGE,
    DEMO_USER_ID,
    DemoRateLimiter,
    DemoSessionLease,
    trusted_client_ip,
)
from demo_reset import DemoResetResult, DemoResetStatus
from demo_store import DemoPersistentStore


SECRET = "demo-hardening-test-secret-at-least-32-chars"


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeOwnership:
    def __init__(self, session_ids: list[str] | None = None) -> None:
        self.session_ids = session_ids or []
        self.cleared: list[str] = []

    async def claim(self, session_id: str, user_id: str) -> bool:
        del session_id, user_id
        return True

    async def session_ids_for_user(self, user_id: str) -> list[str]:
        self.last_user = user_id
        return list(self.session_ids)

    async def clear_demo_state(
        self,
        *,
        user_id: str,
        thread_ids: list[str],
    ) -> None:
        self.last_user = user_id
        self.cleared = list(thread_ids)


class FakeActionStore:
    def __init__(
        self,
        *,
        blockers: list[dict[str, object]] | None = None,
        actions: list[dict[str, object]] | None = None,
    ) -> None:
        self.blockers = blockers or []
        self.actions = actions or []
        self.clear_calls = 0

    def demo_reset_blockers(self, *, user_id: str) -> list[dict[str, object]]:
        self.last_user = user_id
        return list(self.blockers)

    def list_actions(self) -> list[dict[str, object]]:
        return list(self.actions)

    def clear_demo_actions(self, *, user_id: str) -> dict[str, object]:
        self.last_user = user_id
        self.clear_calls += 1
        return {"cleared": True, "action_ids": [], "session_ids": []}


async def _state_for_reset(
    *,
    clock: MutableClock | None = None,
    action_store: FakeActionStore | None = None,
) -> tuple[SimpleNamespace, dict[str, object]]:
    lease = DemoSessionLease(
        ttl_seconds=1800,
        idle_timeout_seconds=300,
        clock=clock or MutableClock(),
    )
    grant = await lease.issue(jwt_secret=SECRET)
    state = SimpleNamespace(
        action_store=action_store or FakeActionStore(),
        session_ownership=FakeOwnership(["chat-1"]),
        demo_lease=lease,
    )
    return SimpleNamespace(state=state), grant


class DemoHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_01_payload_session_id_cannot_bypass_chat_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict("os.environ", {"DEMO_ENABLED": "true"}):
                app = api.create_app()
            app.state.jwt_secret = SECRET
            app.state.demo_lease = DemoSessionLease()
            grant = await app.state.demo_lease.issue(jwt_secret=SECRET)
            app.state.demo_rate_limiter = DemoRateLimiter(limit=1)
            app.state.demo_store = DemoPersistentStore(
                Path(temporary) / "demo.sqlite"
            )
            app.state.demo_daily_token_limit = 500_000
            app.state.demo_usage_available = True
            app.state.demo_trust_proxy_headers = False
            app.state.demo_trusted_proxy_ips = {"127.0.0.1", "::1"}
            key = (
                f"{(await app.state.demo_lease.snapshot()).lease_id}|"
                "127.0.0.1"
            )
            self.assertTrue(await app.state.demo_rate_limiter.allow(key))
            transport = httpx.ASGITransport(
                app=app, client=("127.0.0.1", 1234)
            )
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                headers = {"Authorization": f"Bearer {grant['token']}"}
                first = await client.post(
                    "/api/v1/chat/stream",
                    headers=headers,
                    json={"session_id": "attacker-a", "message": "问题"},
                )
                second = await client.post(
                    "/api/v1/chat/stream",
                    headers=headers,
                    json={"session_id": "attacker-b", "message": "问题"},
                )
            self.assertEqual(first.status_code, 429)
            self.assertEqual(second.status_code, 429)

    async def test_02_untrusted_forwarded_for_is_ignored(self) -> None:
        self.assertEqual(
            trusted_client_ip(
                direct_ip="203.0.113.7",
                forwarded_for="198.51.100.8",
                trust_proxy_headers=True,
                trusted_proxy_ips={"127.0.0.1"},
            ),
            "203.0.113.7",
        )

    async def test_03_trusted_proxy_uses_first_forwarded_ip(self) -> None:
        self.assertEqual(
            trusted_client_ip(
                direct_ip="127.0.0.1",
                forwarded_for="198.51.100.8, 10.0.0.2",
                trust_proxy_headers=True,
                trusted_proxy_ips={"127.0.0.1"},
            ),
            "198.51.100.8",
        )

    async def test_04_eleventh_session_request_is_limited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DemoPersistentStore(Path(temporary) / "demo.sqlite")
            results = [
                store.allow_session_request("203.0.113.9", now=1000.0)
                for _ in range(11)
            ]
            self.assertEqual(results, [True] * 10 + [False])

    async def test_05_idle_lease_resets_before_new_grant(self) -> None:
        clock = MutableClock()
        app, old = await _state_for_reset(clock=clock)
        clock.value += 301
        snapshot = await app.state.demo_lease.snapshot()
        with patch.object(
            api,
            "reset_demo_java",
            return_value=DemoResetResult(DemoResetStatus.SUCCESS, "0000"),
        ):
            await api._reset_demo_lease(
                app, lease_id=snapshot.lease_id, request_id="idle"
            )
        new = await app.state.demo_lease.issue(jwt_secret=SECRET)
        self.assertNotEqual(old["lease_token"], new["lease_token"])

    async def test_06_absolute_expiry_resets_before_new_grant(self) -> None:
        clock = MutableClock()
        app, old = await _state_for_reset(clock=clock)
        clock.value += 1801
        snapshot = await app.state.demo_lease.snapshot()
        with patch.object(
            api,
            "reset_demo_java",
            return_value=DemoResetResult(DemoResetStatus.SUCCESS, "0000"),
        ):
            await api._reset_demo_lease(
                app, lease_id=snapshot.lease_id, request_id="absolute"
            )
        new = await app.state.demo_lease.issue(jwt_secret=SECRET)
        self.assertNotEqual(old["lease_token"], new["lease_token"])

    async def test_07_release_cleans_local_state_and_lease(self) -> None:
        actions = [
            {
                "action_id": "act-1",
                "session_id": "chat-1",
                "user_id": DEMO_USER_ID,
            }
        ]
        action_store = FakeActionStore(actions=actions)
        app, _ = await _state_for_reset(action_store=action_store)
        snapshot = await app.state.demo_lease.snapshot()
        with patch.object(
            api,
            "reset_demo_java",
            return_value=DemoResetResult(DemoResetStatus.SUCCESS, "0000"),
        ):
            await api._reset_demo_lease(
                app, lease_id=snapshot.lease_id, request_id="release"
            )
        self.assertEqual(action_store.clear_calls, 1)
        self.assertIn("chat-1", app.state.session_ownership.cleared)
        self.assertIn("action:act-1", app.state.session_ownership.cleared)
        self.assertIsNone(await app.state.demo_lease.snapshot())

    async def test_08_reset_client_success_allows_fresh_dataset(self) -> None:
        app, _ = await _state_for_reset()
        snapshot = await app.state.demo_lease.snapshot()
        with patch.object(
            api,
            "reset_demo_java",
            return_value=DemoResetResult(DemoResetStatus.SUCCESS, "0000"),
        ) as reset:
            await api._reset_demo_lease(
                app, lease_id=snapshot.lease_id, request_id="smoke-reset"
            )
        reset.assert_called_once_with(request_id="smoke-reset")

    async def test_09_active_refund_blocks_release_and_keeps_lease(self) -> None:
        for action_status in (ActionStatus.EXECUTING, ActionStatus.UNKNOWN):
            blocker = [{"status": action_status.value}]
            app, _ = await _state_for_reset(
                action_store=FakeActionStore(blockers=blocker)
            )
            snapshot = await app.state.demo_lease.snapshot()
            with self.assertRaises(api.HTTPException) as raised:
                await api._reset_demo_lease(
                    app, lease_id=snapshot.lease_id, request_id="blocked"
                )
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIsNotNone(await app.state.demo_lease.snapshot())

    async def test_10_java_blocked_keeps_lease(self) -> None:
        app, _ = await _state_for_reset()
        snapshot = await app.state.demo_lease.snapshot()
        with patch.object(
            api,
            "reset_demo_java",
            return_value=DemoResetResult(
                DemoResetStatus.BLOCKED, "DEMO_RESET_BLOCKED"
            ),
        ), self.assertRaises(api.HTTPException) as raised:
            await api._reset_demo_lease(
                app, lease_id=snapshot.lease_id, request_id="java-blocked"
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIsNotNone(await app.state.demo_lease.snapshot())

    async def test_11_java_network_failure_keeps_lease(self) -> None:
        app, _ = await _state_for_reset()
        snapshot = await app.state.demo_lease.snapshot()
        with patch.object(
            api,
            "reset_demo_java",
            return_value=DemoResetResult(
                DemoResetStatus.UNAVAILABLE, "REQUEST_FAILED"
            ),
        ), self.assertRaises(api.HTTPException) as raised:
            await api._reset_demo_lease(
                app, lease_id=snapshot.lease_id, request_id="java-down"
            )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIsNotNone(await app.state.demo_lease.snapshot())

    async def test_12_quota_blocks_before_agent_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict("os.environ", {"DEMO_ENABLED": "true"}):
                app = api.create_app()
            store = DemoPersistentStore(Path(temporary) / "demo.sqlite")
            store.add_token_usage(
                input_tokens=8, output_tokens=2, total_tokens=10
            )
            app.state.jwt_secret = SECRET
            app.state.demo_lease = DemoSessionLease()
            grant = await app.state.demo_lease.issue(jwt_secret=SECRET)
            app.state.demo_rate_limiter = DemoRateLimiter()
            app.state.demo_store = store
            app.state.demo_daily_token_limit = 10
            app.state.demo_usage_available = True
            app.state.demo_trust_proxy_headers = False
            app.state.demo_trusted_proxy_ips = {"127.0.0.1"}
            mocked = AsyncMock()
            with patch.object(api, "_run_agent", mocked):
                transport = httpx.ASGITransport(
                    app=app, client=("127.0.0.1", 1234)
                )
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.post(
                        "/api/v1/chat/stream",
                        headers={
                            "Authorization": f"Bearer {grant['token']}"
                        },
                        json={"session_id": "quota", "message": "问题"},
                    )
            self.assertEqual(response.status_code, 200)
            self.assertIn(DEMO_QUOTA_EXHAUSTED_MESSAGE, response.text)
            mocked.assert_not_awaited()

    async def test_13_quota_persists_across_store_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite"
            first = DemoPersistentStore(path)
            first.add_token_usage(
                input_tokens=7, output_tokens=3, total_tokens=10
            )
            second = DemoPersistentStore(path)
            self.assertEqual(second.token_usage().total_tokens, 10)

    async def test_14_reset_does_not_clear_quota_or_ip_counter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = DemoPersistentStore(Path(temporary) / "demo.sqlite")
            store.add_token_usage(
                input_tokens=7, output_tokens=3, total_tokens=10
            )
            for _ in range(10):
                self.assertTrue(
                    store.allow_session_request("203.0.113.2", now=1000.0)
                )
            app, _ = await _state_for_reset()
            snapshot = await app.state.demo_lease.snapshot()
            with patch.object(
                api,
                "reset_demo_java",
                return_value=DemoResetResult(DemoResetStatus.SUCCESS, "0000"),
            ):
                await api._reset_demo_lease(
                    app, lease_id=snapshot.lease_id, request_id="reset"
                )
            self.assertEqual(store.token_usage().total_tokens, 10)
            self.assertFalse(
                store.allow_session_request("203.0.113.2", now=1000.0)
            )

    async def test_15_non_demo_routes_are_not_registered(self) -> None:
        with patch.dict("os.environ", {"DEMO_ENABLED": "false"}):
            app = api.create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            session = await client.post("/demo/session")
            release = await client.post("/demo/release")
        self.assertEqual(session.status_code, 404)
        self.assertEqual(release.status_code, 404)


class LedgerDemoResetTests(unittest.TestCase):
    def test_confirmed_executing_unknown_are_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AgentActionStore(Path(temporary) / "actions.sqlite")
            connection = store._connect()
            try:
                for index, status in enumerate(
                    ("CONFIRMED", "EXECUTING", "UNKNOWN")
                ):
                    connection.execute(
                        """
                        INSERT INTO agent_action (
                            action_id, session_id, user_id, action_type,
                            out_trade_no, status, preview_json,
                            expected_version, idempotency_key, args_hash,
                            expires_at, version, created_at, updated_at
                        ) VALUES (?, ?, ?, 'REFUND', '930000000001', ?, '{}',
                                  'v', 'key', 'hash', '2099-01-01', 1,
                                  '2026-01-01', '2026-01-01')
                        """,
                        (f"a{index}", f"s{index}", DEMO_USER_ID, status),
                    )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(len(store.demo_reset_blockers(user_id=DEMO_USER_ID)), 3)


if __name__ == "__main__":
    unittest.main()
