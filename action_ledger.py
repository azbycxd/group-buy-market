from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ACTION_DB_PATH = PROJECT_ROOT / "data" / "agent_actions.sqlite"


AGENT_ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_action (
    action_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    out_trade_no TEXT NOT NULL,
    status TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    expected_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def action_db_path() -> Path:
    return Path(
        os.getenv("AGENT_ACTION_DB_PATH", str(DEFAULT_ACTION_DB_PATH))
    ).resolve()


class AgentActionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path else action_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(AGENT_ACTION_SCHEMA)
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def create_refund_proposal(
        self,
        *,
        session_id: str,
        user_id: str,
        out_trade_no: str,
        preview: dict[str, Any],
        expected_version: str,
    ) -> str:
        action_id = f"act_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO agent_action (
                    action_id, session_id, user_id, action_type,
                    out_trade_no, status, preview_json,
                    expected_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    session_id,
                    user_id,
                    "REFUND",
                    out_trade_no,
                    "PROPOSED",
                    json.dumps(
                        preview,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    expected_version,
                    now,
                    now,
                ),
            )
            connection.commit()
        return action_id

    def list_actions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM agent_action ORDER BY created_at, action_id"
            ).fetchall()
        return [dict(row) for row in rows]
