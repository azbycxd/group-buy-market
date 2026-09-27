import os
from dataclasses import dataclass

import httpx
from langchain.tools import ToolRuntime, tool


@dataclass
class AgentContext:
    user_id: str


@tool
def get_order_facts(
    outTradeNo: str,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """查询订单事实；参数是要查询的外部交易订单号。"""
    base_url = os.getenv("FAKE_JAVA_BASE_URL", "http://127.0.0.1:8000")
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/v1/agent/order/facts",
        json={"outTradeNo": outTradeNo},
        headers={
            "X-Dev-Authenticated-User-Id": runtime.context.user_id,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()
