from __future__ import annotations

import asyncio


class SessionLockRegistry:
    """Process-local, fail-fast locks keyed by LangGraph thread id."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def try_acquire(self, session_id: str) -> asyncio.Lock | None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            return None
        await lock.acquire()
        return lock

    def release(self, session_id: str, lock: asyncio.Lock) -> None:
        if self._locks.get(session_id) is lock and lock.locked():
            lock.release()
