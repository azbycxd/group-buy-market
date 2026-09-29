from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from demo import DEMO_SESSION_IP_LIMIT, DEMO_SESSION_IP_WINDOW_SECONDS


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DEMO_DB_PATH = PROJECT_ROOT / "data" / "demo_state.sqlite"


@dataclass(frozen=True)
class DailyTokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


def demo_db_path() -> Path:
    return Path(
        os.getenv("DEMO_STATE_DB_PATH", str(DEFAULT_DEMO_DB_PATH))
    ).resolve()


class DemoPersistentStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path else demo_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS demo_session_ip_request (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_ip TEXT NOT NULL,
                    requested_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_demo_session_ip_request
                ON demo_session_ip_request (client_ip, requested_at);

                CREATE TABLE IF NOT EXISTS demo_daily_token_usage (
                    usage_date TEXT PRIMARY KEY NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def allow_session_request(
        self,
        client_ip: str,
        *,
        now: float | None = None,
        limit: int = DEMO_SESSION_IP_LIMIT,
        window_seconds: float = DEMO_SESSION_IP_WINDOW_SECONDS,
    ) -> bool:
        current = time.time() if now is None else now
        cutoff = current - window_seconds
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM demo_session_ip_request WHERE requested_at <= ?",
                (cutoff,),
            )
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM demo_session_ip_request
                    WHERE client_ip = ? AND requested_at > ?
                    """,
                    (client_ip, cutoff),
                ).fetchone()[0]
            )
            if count >= limit:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO demo_session_ip_request (client_ip, requested_at)
                VALUES (?, ?)
                """,
                (client_ip, current),
            )
            connection.commit()
            return True

    @staticmethod
    def utc_usage_date(now: datetime | None = None) -> str:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).date().isoformat()

    def token_usage(self, usage_date: str | None = None) -> DailyTokenUsage:
        selected_date = usage_date or self.utc_usage_date()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT input_tokens, output_tokens, total_tokens
                FROM demo_daily_token_usage WHERE usage_date = ?
                """,
                (selected_date,),
            ).fetchone()
        if row is None:
            return DailyTokenUsage(0, 0, 0)
        return DailyTokenUsage(int(row[0]), int(row[1]), int(row[2]))

    def add_token_usage(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int,
        usage_date: str | None = None,
    ) -> DailyTokenUsage:
        selected_date = usage_date or self.utc_usage_date()
        input_value = input_tokens or 0
        output_value = output_tokens or 0
        updated_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO demo_daily_token_usage (
                    usage_date, input_tokens, output_tokens,
                    total_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(usage_date) DO UPDATE SET
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    total_tokens = total_tokens + excluded.total_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    selected_date,
                    input_value,
                    output_value,
                    total_tokens,
                    updated_at,
                ),
            )
            connection.commit()
        return self.token_usage(selected_date)
