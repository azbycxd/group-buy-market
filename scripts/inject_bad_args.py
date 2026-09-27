from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from agent_middleware import create_agent_middleware
from fake_java.main import app
from tools.activity_facts import get_activity_facts
from tools.context import AgentContext
from understanding import EntityMention, EntityType, resolve_entities


REQUEST_COUNT = 0


@app.middleware("http")
async def count_facts_requests(request: Any, call_next: Any) -> Any:
    global REQUEST_COUNT
    if request.url.path.startswith("/api/v1/agent/"):
        REQUEST_COUNT += 1
    return await call_next(request)


class ScriptedToolModel(GenericFakeChatModel):
    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "ScriptedToolModel":
        return self


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("等待 fake_java 启动超时")


def scripted_agent(activity_id: Any, call_id: str) -> Any:
    model = ScriptedToolModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_activity_facts",
                            "args": {"activityId": activity_id},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="脚本测试结束。"),
            ]
        )
    )
    return create_agent(
        model=model,
        tools=[get_activity_facts],
        middleware=create_agent_middleware(),
        context_schema=AgentContext,
    )


def run_case(
    *,
    user_text: str,
    mentions: list[EntityMention],
    activity_id: Any,
    call_id: str,
) -> tuple[list[ToolMessage], int]:
    global REQUEST_COUNT
    REQUEST_COUNT = 0
    entities = resolve_entities(user_text, mentions)
    result = scripted_agent(activity_id, call_id).invoke(
        {"messages": [{"role": "user", "content": user_text}]},
        context=AgentContext(
            user_id="demo-user",
            parsed_entities=tuple(entities),
            user_text=user_text,
        ),
    )
    tool_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    return tool_messages, REQUEST_COUNT


def result_code(messages: list[ToolMessage]) -> str:
    content = str(messages[-1].content) if messages else ""
    if "GROUNDING_BLOCKED" in content:
        return "GROUNDING_BLOCKED"
    if '"code": "0000"' in content or "'code': '0000'" in content:
        return "0000"
    return "UNKNOWN"


def main() -> None:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    original_base_url = os.environ.get("FAKE_JAVA_BASE_URL")
    thread.start()
    wait_until_ready(port)
    os.environ["FAKE_JAVA_BASE_URL"] = f"http://127.0.0.1:{port}"

    try:
        bad_value_messages, bad_value_count = run_case(
            user_text="请查询 activityId=100123 的活动状态",
            mentions=[
                EntityMention(
                    entity_type=EntityType.ACTIVITY,
                    source_text="activityId=100123",
                )
            ],
            activity_id=200456,
            call_id="bad-value",
        )
        print(
            "BAD_ACTIVITY_ID:",
            result_code(bad_value_messages),
            f"fake_java_requests={bad_value_count}",
        )

        wrong_semantic_messages, wrong_semantic_count = run_case(
            user_text="请查询订单号 ORD100001",
            mentions=[
                EntityMention(
                    entity_type=EntityType.ORDER,
                    source_text="ORD100001",
                )
            ],
            activity_id="ORD100001",
            call_id="wrong-semantic",
        )
        print(
            "WRONG_SEMANTIC:",
            result_code(wrong_semantic_messages),
            f"fake_java_requests={wrong_semantic_count}",
        )

        normal_messages, normal_count = run_case(
            user_text="请查询 activityId=100123 的活动状态",
            mentions=[
                EntityMention(
                    entity_type=EntityType.ACTIVITY,
                    source_text="activityId=100123",
                )
            ],
            activity_id=100123,
            call_id="normal",
        )
        print(
            "NORMAL_ACTIVITY_ID:",
            result_code(normal_messages),
            f"fake_java_requests={normal_count}",
        )

        overlap_messages, overlap_count = run_case(
            user_text="订单 ORD100123 的活动 100123 还有效吗",
            mentions=[
                EntityMention(
                    entity_type=EntityType.ORDER,
                    source_text="ORD100123",
                ),
                EntityMention(
                    entity_type=EntityType.ACTIVITY,
                    source_text="活动 100123",
                ),
            ],
            activity_id=100123,
            call_id="overlapping-number",
        )
        print(
            "OVERLAPPING_NUMBER:",
            result_code(overlap_messages),
            f"fake_java_requests={overlap_count}",
        )
    finally:
        if original_base_url is None:
            os.environ.pop("FAKE_JAVA_BASE_URL", None)
        else:
            os.environ["FAKE_JAVA_BASE_URL"] = original_base_url
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
