import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, ToolMessage

from agent import create_order_agent
from tools.order_facts import AgentContext


def _format_tool_call(name: str, args: dict[str, Any]) -> str:
    rendered_args = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in args.items()
    )
    return f"调用 {name}({rendered_args})"


def _tool_status(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("status", content))
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, dict):
            return str(parsed.get("status", parsed))
    return str(content)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('用法: python cli.py "订单 ORD100001 现在什么状态"')

    load_dotenv()
    agent = create_order_agent()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": sys.argv[1]}]},
        context=AgentContext(user_id=os.getenv("AGENT_USER_ID", "demo-user")),
    )

    final_answer: Any = ""
    for message in result["messages"]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                print(_format_tool_call(call["name"], call["args"]))
            if not message.tool_calls and message.content:
                final_answer = message.content
        elif isinstance(message, ToolMessage):
            print(f"返回 status={_tool_status(message.content)}")

    if isinstance(final_answer, str):
        print(f"最终回答: {final_answer}")
    else:
        print(f"最终回答: {json.dumps(final_answer, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
