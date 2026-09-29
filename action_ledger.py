from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ACTION_DB_PATH = PROJECT_ROOT / "data" / "agent_actions.sqlite"
PROPOSAL_TTL = timedelta(minutes=5)


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    EXECUTING = "EXECUTING"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS = {
    (ActionStatus.PROPOSED, ActionStatus.CONFIRMED),
    (ActionStatus.CONFIRMED, ActionStatus.FAILED),
    (ActionStatus.CONFIRMED, ActionStatus.EXECUTING),
    (ActionStatus.EXECUTING, ActionStatus.UNKNOWN),
    (ActionStatus.EXECUTING, ActionStatus.SUCCEEDED),
    (ActionStatus.EXECUTING, ActionStatus.FAILED),
    (ActionStatus.UNKNOWN, ActionStatus.SUCCEEDED),
    (ActionStatus.UNKNOWN, ActionStatus.FAILED),
}


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
    idempotency_key TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    result_code TEXT,
    refund_executed INTEGER,
    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
    needs_manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

AGENT_ACTION_MIGRATIONS = {
    "idempotency_key": (
        "ALTER TABLE agent_action "
        "ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
    ),
    "args_hash": (
        "ALTER TABLE agent_action "
        "ADD COLUMN args_hash TEXT NOT NULL DEFAULT ''"
    ),
    "expires_at": (
        "ALTER TABLE agent_action "
        "ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''"
    ),
    "version": (
        "ALTER TABLE agent_action "
        "ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
    ),
    "result_code": "ALTER TABLE agent_action ADD COLUMN result_code TEXT",
    "refund_executed": (
        "ALTER TABLE agent_action ADD COLUMN refund_executed INTEGER"
    ),
    "reconcile_attempts": (
        "ALTER TABLE agent_action "
        "ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0"
    ),
    "needs_manual": (
        "ALTER TABLE agent_action "
        "ADD COLUMN needs_manual INTEGER NOT NULL DEFAULT 0"
    ),
}


def action_db_path() -> Path:
    return Path(
        os.getenv("AGENT_ACTION_DB_PATH", str(DEFAULT_ACTION_DB_PATH))
    ).resolve()


def required_confirmation_secret() -> str:
    secret = os.getenv("ACTION_CONFIRM_SECRET")
    if not secret:
        raise RuntimeError("缺少环境变量: ACTION_CONFIRM_SECRET")
    return secret


def refund_args_hash(
    *,
    out_trade_no: str,
    refund_type: str,
    expected_version: str,
) -> str:
    serialized = json.dumps(
        {
            "expectedVersion": expected_version,
            "outTradeNo": out_trade_no,
            "refundType": refund_type,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def action_args_hash_is_current(action: dict[str, Any]) -> bool:
    try:
        preview = json.loads(str(action["preview_json"]))
        expected = refund_args_hash(
            out_trade_no=str(action["out_trade_no"]),
            refund_type=str(preview["refundType"]),
            expected_version=str(action["expected_version"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return hmac.compare_digest(str(action["args_hash"]), expected)


def confirmation_credential(
    action: dict[str, Any],
    *,
    secret: str | None = None,
) -> str:
    signing_secret = secret or required_confirmation_secret()
    payload = "".join(
        str(action[field])
        for field in ("action_id", "args_hash", "user_id", "expires_at")
    )
    return hmac.new(
        signing_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def credential_matches(
    action: dict[str, Any],
    credential: str,
    *,
    secret: str | None = None,
) -> bool:
    expected = confirmation_credential(action, secret=secret)
    return hmac.compare_digest(expected, credential)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat()


class AgentActionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path else action_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(AGENT_ACTION_SCHEMA)
            existing_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_action)"
                ).fetchall()
            }
            for column, statement in AGENT_ACTION_MIGRATIONS.items():
                if column not in existing_columns:
                    connection.execute(statement)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_action_open_proposal
                ON agent_action (
                    user_id, session_id, out_trade_no, status, expires_at
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def create_or_reuse_refund_proposal(
        self,
        *,
        session_id: str,
        user_id: str,
        out_trade_no: str,
        preview: dict[str, Any],
        expected_version: str,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        current_time = _as_utc(now or _utc_now())
        now_text = _timestamp(current_time)
        expires_at = _timestamp(current_time + PROPOSAL_TTL)
        refund_type = preview.get("refundType")
        if not isinstance(refund_type, str) or not refund_type:
            raise ValueError("退款预览缺少 refundType")

        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM agent_action
                    WHERE user_id = ?
                      AND session_id = ?
                      AND out_trade_no = ?
                      AND status = 'PROPOSED'
                      AND expires_at > ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id, session_id, out_trade_no, now_text),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return dict(existing), False

                action_id = f"act_{uuid.uuid4().hex}"
                idempotency_key = f"refund_{uuid.uuid4().hex}"
                args_hash = refund_args_hash(
                    out_trade_no=out_trade_no,
                    refund_type=refund_type,
                    expected_version=expected_version,
                )
                connection.execute(
                    """
                    INSERT INTO agent_action (
                        action_id, session_id, user_id, action_type,
                        out_trade_no, status, preview_json,
                        expected_version, idempotency_key, args_hash,
                        expires_at, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        session_id,
                        user_id,
                        "REFUND",
                        out_trade_no,
                        ActionStatus.PROPOSED.value,
                        json.dumps(
                            preview,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        expected_version,
                        idempotency_key,
                        args_hash,
                        expires_at,
                        1,
                        now_text,
                        now_text,
                    ),
                )
                created = connection.execute(
                    "SELECT * FROM agent_action WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                connection.commit()
                if created is None:
                    raise sqlite3.DatabaseError("退款提议写入后无法读取")
                return dict(created), True
            except Exception:
                connection.rollback()
                raise

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM agent_action WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def confirm_refund_proposal(
        self,
        *,
        action_id: str,
        user_id: str,
        expected_version: int,
        now: datetime | None = None,
    ) -> bool:
        now_text = _timestamp(now or _utc_now())
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE agent_action
                SET status = ?,
                    version = version + 1,
                    updated_at = ?
                WHERE action_id = ?
                  AND user_id = ?
                  AND status = ?
                  AND expires_at > ?
                  AND version = ?
                """,
                (
                    ActionStatus.CONFIRMED.value,
                    now_text,
                    action_id,
                    user_id,
                    ActionStatus.PROPOSED.value,
                    now_text,
                    expected_version,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def transition_status(
        self,
        *,
        action_id: str,
        from_status: ActionStatus,
        to_status: ActionStatus,
        expected_version: int,
        result_code: str | None = None,
        refund_executed: bool | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if (from_status, to_status) not in ALLOWED_TRANSITIONS:
            raise ValueError(
                f"不允许的 action 状态变化: {from_status} -> {to_status}"
            )
        now_text = _timestamp(now or _utc_now())
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE agent_action
                    SET status = ?,
                        result_code = ?,
                        refund_executed = ?,
                        version = version + 1,
                        updated_at = ?
                    WHERE action_id = ?
                      AND status = ?
                      AND version = ?
                    """,
                    (
                        to_status.value,
                        result_code,
                        (
                            None
                            if refund_executed is None
                            else int(refund_executed)
                        ),
                        now_text,
                        action_id,
                        from_status.value,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                row = connection.execute(
                    "SELECT * FROM agent_action WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                connection.commit()
                return dict(row) if row is not None else None
            except Exception:
                connection.rollback()
                raise

    def list_actions(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM agent_action ORDER BY created_at, action_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def demo_reset_blockers(self, *, user_id: str) -> list[dict[str, Any]]:
        """Return actions that make Demo reset unsafe."""
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM agent_action
                WHERE user_id = ?
                  AND status IN ('CONFIRMED', 'EXECUTING', 'UNKNOWN')
                ORDER BY updated_at, action_id
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_demo_actions(self, *, user_id: str) -> dict[str, Any]:
        """Delete only safe, fixed-user Demo actions after Java reset."""
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                blockers = connection.execute(
                    """
                    SELECT action_id FROM agent_action
                    WHERE user_id = ?
                      AND status IN ('CONFIRMED', 'EXECUTING', 'UNKNOWN')
                    """,
                    (user_id,),
                ).fetchall()
                if blockers:
                    connection.rollback()
                    return {
                        "cleared": False,
                        "action_ids": [],
                        "session_ids": [],
                    }
                rows = connection.execute(
                    """
                    SELECT action_id, session_id FROM agent_action
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchall()
                connection.execute(
                    "DELETE FROM agent_action WHERE user_id = ?",
                    (user_id,),
                )
                connection.commit()
                return {
                    "cleared": True,
                    "action_ids": [str(row["action_id"]) for row in rows],
                    "session_ids": sorted(
                        {str(row["session_id"]) for row in rows}
                    ),
                }
            except Exception:
                connection.rollback()
                raise

    def list_stuck_actions(
        self,
        *,
        before: datetime,
    ) -> list[dict[str, Any]]:
        cutoff = _timestamp(before)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM agent_action
                WHERE status IN ('CONFIRMED', 'EXECUTING', 'UNKNOWN')
                  AND updated_at < ?
                  AND needs_manual = 0
                ORDER BY updated_at, action_id
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_unknown_reconcile_attempt(
        self,
        *,
        action_id: str,
        expected_version: int,
        max_attempts: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        now_text = _timestamp(now or _utc_now())
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE agent_action
                    SET reconcile_attempts = reconcile_attempts + 1,
                        needs_manual = CASE
                            WHEN reconcile_attempts + 1 >= ? THEN 1
                            ELSE needs_manual
                        END,
                        version = version + 1,
                        updated_at = ?
                    WHERE action_id = ?
                      AND status = 'UNKNOWN'
                      AND version = ?
                      AND needs_manual = 0
                    """,
                    (
                        max_attempts,
                        now_text,
                        action_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                row = connection.execute(
                    "SELECT * FROM agent_action WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                connection.commit()
                return dict(row) if row is not None else None
            except Exception:
                connection.rollback()
                raise
