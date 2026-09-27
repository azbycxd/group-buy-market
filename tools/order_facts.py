from langchain.tools import ToolRuntime, tool

from tools.arguments import OrderFactsArguments
from tools.context import AgentContext
from tools.facts import OrderFacts
from tools.facts_client import query_facts


@tool
def get_order_facts(
    outTradeNo: str,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """查询订单事实；参数是要查询的外部交易订单号。"""
    arguments = OrderFactsArguments.model_validate({"outTradeNo": outTradeNo})
    return query_facts(
        path="/api/v1/agent/order/facts",
        body={"outTradeNo": arguments.outTradeNo},
        user_id=runtime.context.user_id,
        request_id=runtime.context.request_id,
        data_model=OrderFacts,
    )
