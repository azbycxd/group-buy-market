from __future__ import annotations

from pathlib import Path

import aiosqlite


class SessionOwnershipStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    @classmethod
    async def open(cls, database_path: str | Path) -> SessionOwnershipStore:
        connection = await aiosqlite.connect(Path(database_path).resolve())
        store = cls(connection)
        await store._initialize()
        return store

    async def _initialize(self) -> None:
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_session_ownership (
                session_id TEXT PRIMARY KEY NOT NULL,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await self._connection.commit()

    async def claim(self, session_id: str, user_id: str) -> bool:
        await self._connection.execute(
            """
            INSERT INTO agent_session_ownership (session_id, user_id)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (session_id, user_id),
        )
        await self._connection.commit()
        cursor = await self._connection.execute(
            """
            SELECT user_id
            FROM agent_session_ownership
            WHERE session_id = ?
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None and row[0] == user_id

    async def close(self) -> None:
        await self._connection.close()
