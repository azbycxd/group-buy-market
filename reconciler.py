from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from action_ledger import ActionStatus, AgentActionStore
from tools.refund_execute import ExecutionCertainty
from tools.refund_result import query_refund_result


logger = logging.getLogger("group_buy_agent.reconciler")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _transition(
    store: AgentActionStore,
    action: dict[str, Any],
    to_status: ActionStatus,
    *,
    result_code: str | None = None,
    refund_executed: bool | None = None,
    now: datetime,
) -> dict[str, Any] | None:
    return store.transition_status(
        action_id=str(action["action_id"]),
        from_status=ActionStatus(str(action["status"])),
        to_status=to_status,
        expected_version=int(action["version"]),
        result_code=result_code,
        refund_executed=refund_executed,
        now=now,
    )


def reconcile_stuck_actions(
    now: datetime | None = None,
    min_age: float = 60,
    max_attempts: int = 5,
) -> dict[str, int]:
    if min_age < 0:
        raise ValueError("min_age 不能小于 0")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")

    current_time = _as_utc(now or datetime.now(timezone.utc))
    store = AgentActionStore()
    actions = store.list_stuck_actions(
        before=current_time - timedelta(seconds=min_age),
    )
    summary = {
        "scanned": len(actions),
        "succeeded": 0,
        "failed": 0,
        "still_unknown": 0,
        "manual": 0,
        "cas_skipped": 0,
    }

    for scanned in actions:
        action = scanned
        status = ActionStatus(str(action["status"]))

        if status is ActionStatus.CONFIRMED:
            failed = _transition(
                store,
                action,
                ActionStatus.FAILED,
                result_code="CONFIRMED_NOT_EXECUTED",
                refund_executed=False,
                now=current_time,
            )
            if failed is None:
                summary["cas_skipped"] += 1
            else:
                summary["failed"] += 1
            continue

        if status is ActionStatus.EXECUTING:
            unknown = _transition(
                store,
                action,
                ActionStatus.UNKNOWN,
                result_code=(
                    str(action["result_code"])
                    if action.get("result_code")
                    else "STALE_EXECUTING"
                ),
                refund_executed=None,
                now=current_time,
            )
            if unknown is None:
                summary["cas_skipped"] += 1
                continue
            action = unknown

        request_id = f"reconcile-{uuid.uuid4().hex}"
        result = query_refund_result(action, request_id=request_id)
        if result.certainty is ExecutionCertainty.SUCCESS:
            terminal = _transition(
                store,
                action,
                ActionStatus.SUCCEEDED,
                result_code=result.result_code,
                refund_executed=result.refund_executed,
                now=current_time,
            )
            if terminal is None:
                summary["cas_skipped"] += 1
            else:
                summary["succeeded"] += 1
            continue

        if result.certainty is ExecutionCertainty.FAILURE:
            terminal = _transition(
                store,
                action,
                ActionStatus.FAILED,
                result_code=result.result_code,
                refund_executed=result.refund_executed,
                now=current_time,
            )
            if terminal is None:
                summary["cas_skipped"] += 1
            else:
                summary["failed"] += 1
            continue

        updated = store.record_unknown_reconcile_attempt(
            action_id=str(action["action_id"]),
            expected_version=int(action["version"]),
            max_attempts=max_attempts,
            now=current_time,
        )
        if updated is None:
            summary["cas_skipped"] += 1
            continue
        summary["still_unknown"] += 1
        if int(updated["needs_manual"]) == 1:
            summary["manual"] += 1
            logger.error(
                "refund_reconcile_needs_manual action_id=%s attempts=%s",
                updated["action_id"],
                updated["reconcile_attempts"],
            )

    return summary


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="执行一次退款 action 只读对账扫描",
    )
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    if not arguments.once:
        parser.error("当前只支持 --once")
    print(json.dumps(reconcile_stuck_actions(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
