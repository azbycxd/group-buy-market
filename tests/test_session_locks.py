from __future__ import annotations

import asyncio
import unittest

from session_locks import SessionLockRegistry


class SessionLockRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_session_is_fail_fast_and_reusable(self) -> None:
        registry = SessionLockRegistry()
        first = await registry.try_acquire("session-1")
        self.assertIsNotNone(first)
        competing = await asyncio.gather(
            *(registry.try_acquire("session-1") for _ in range(19))
        )
        self.assertTrue(all(item is None for item in competing))
        registry.release("session-1", first)
        next_request = await registry.try_acquire("session-1")
        self.assertIsNotNone(next_request)
        registry.release("session-1", next_request)

    async def test_different_sessions_have_different_locks(self) -> None:
        registry = SessionLockRegistry()
        first, second = await asyncio.gather(
            registry.try_acquire("session-1"),
            registry.try_acquire("session-2"),
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNot(first, second)
        registry.release("session-1", first)
        registry.release("session-2", second)


if __name__ == "__main__":
    unittest.main()
