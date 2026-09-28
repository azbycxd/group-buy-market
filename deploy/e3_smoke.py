from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt
from dotenv import dotenv_values


def _jwt(secret: str, user_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + 3600},
        secret,
        algorithm="HS256",
    )


def _stream(
    client: httpx.Client,
    *,
    token: str,
    session_id: str,
    message: str,
) -> tuple[int, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    event_name = "message"
    data_lines: list[str] = []
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"session_id": session_id, "message": message},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                if data_lines:
                    events.append(
                        {
                            "event": event_name,
                            "data": json.loads("\n".join(data_lines)),
                        }
                    )
                event_name = "message"
                data_lines = []
            elif line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            events.append(
                {
                    "event": event_name,
                    "data": json.loads("\n".join(data_lines)),
                }
            )
    return response.status_code, events


def _summary(status_code: int, events: list[dict[str, Any]]) -> dict[str, Any]:
    final = next(
        (item["data"] for item in events if item["event"] == "final"),
        {},
    )
    return {
        "http_status": status_code,
        "events": [item["event"] for item in events],
        "tools": [
            item["data"].get("tool")
            for item in events
            if item["event"] == "progress"
            and item["data"].get("stage") == "tool"
        ],
        "request_id": final.get("request_id"),
        "outcome": final.get("kind"),
        "answer": final.get("answer"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    arguments = parser.parse_args()
    environment = dotenv_values(arguments.env_file)
    secret = str(environment["JWT_SECRET"])
    user_id = "agent_c3_unpaid"
    token = _jwt(secret, user_id)

    with httpx.Client(
        base_url=arguments.base_url,
        timeout=90,
        trust_env=False,
    ) as client:
        order_status, order_events = _stream(
            client,
            token=token,
            session_id=f"e3-order-{uuid.uuid4().hex}",
            message="查询订单 930000000001 的当前状态。",
        )
        rule_status, rule_events = _stream(
            client,
            token=token,
            session_id=f"e3-rule-{uuid.uuid4().hex}",
            message="钱付了但团没凑齐，这种情况能直接退款吗？",
        )
        proposal_status, proposal_events = _stream(
            client,
            token=token,
            session_id=f"e3-refund-{uuid.uuid4().hex}",
            message="帮我退掉订单930000000001。",
        )
        proposal_event = next(
            item["data"]
            for item in proposal_events
            if item["event"] == "action_proposed"
        )
        action_id = str(proposal_event["action_id"])
        credential = str(proposal_event["credential"])
        proposal_summary = _summary(proposal_status, proposal_events)
        confirm = client.post(
            f"/v1/actions/{action_id}/confirm",
            headers={"Authorization": f"Bearer {token}"},
            json={"credential": credential},
        )
        duplicate = client.post(
            f"/v1/actions/{action_id}/confirm",
            headers={"Authorization": f"Bearer {token}"},
            json={"credential": credential},
        )
        closed_status, closed_events = _stream(
            client,
            token=token,
            session_id=f"e3-closed-{uuid.uuid4().hex}",
            message="查询订单 930000000001 的当前状态。",
        )

    output = {
        "order_query": _summary(order_status, order_events),
        "rule_qa": _summary(rule_status, rule_events),
        "refund_proposal": {
            **proposal_summary,
            "action_id": action_id,
            "preview": proposal_event.get("preview"),
            "credential_in_final_answer": credential in str(
                proposal_summary.get("answer", "")
            ),
        },
        "confirm": {
            "http_status": confirm.status_code,
            "body": confirm.json(),
        },
        "duplicate_confirm": {
            "http_status": duplicate.status_code,
            "body": duplicate.json(),
        },
        "order_after_refund": _summary(closed_status, closed_events),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
