import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, ToolMessage

from agent import DEFAULT_CHECKPOINT_PATH, close_order_agent, create_order_agent
from tools.context import AgentContext


def _format_tool_call(name: str, args: dict[str, Any]) -> str:
    rendered_args = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in args.items()
    )
    return f"调用 {name}({rendered_args})"


def _tool_result(content: Any) -> str:
    parsed = content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content

    if isinstance(parsed, dict):
        if parsed.get("code") != "0000":
            detail = parsed.get("message", parsed.get("info"))
            return f"code={parsed.get('code')}, message={detail}"
        data = parsed.get("data")
        if isinstance(data, dict):
            order = data.get("order")
            if isinstance(order, dict) and "status" in order:
                return f"status={order['status']}"
            if "status" in data:
                return f"status={data['status']}"
    return str(parsed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument(
        "--session-id",
        default=os.getenv("AGENT_SESSION_ID", "cli-default"),
    )
    parser.add_argument(
        "--checkpoint-db",
        type=Path,
        default=Path(
            os.getenv("CHECKPOINT_DB_PATH", str(DEFAULT_CHECKPOINT_PATH))
        ),
    )
    args = parser.parse_args()

    load_dotenv()
    agent = create_order_agent(args.checkpoint_db)
    config = {"configurable": {"thread_id": args.session_id}}
    try:
        snapshot = agent.get_state(config)
        previous_message_count = len(snapshot.values.get("messages", []))
        result = agent.invoke(
            {"messages": [{"role": "user", "content": args.question}]},
            context=AgentContext(
                user_id=os.getenv("AGENT_USER_ID", "demo-user")
            ),
            config=config,
        )
    finally:
        close_order_agent(agent)

    final_answer: Any = ""
    for message in result["messages"][previous_message_count:]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                print(_format_tool_call(call["name"], call["args"]))
            if not message.tool_calls and message.content:
                final_answer = message.content
        elif isinstance(message, ToolMessage):
            print(f"返回 {_tool_result(message.content)}")

    if isinstance(final_answer, str):
        print(f"最终回答: {final_answer}")
    else:
        print(f"最终回答: {json.dumps(final_answer, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
