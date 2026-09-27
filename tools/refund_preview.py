from tools.arguments import OrderFactsArguments
from tools.facts import RefundPreviewFacts
from tools.facts_client import query_facts


def get_refund_preview(
    *,
    out_trade_no: str,
    user_id: str,
    request_id: str = "",
) -> dict[str, object]:
    """Read a refund preview through the fixed proposal workflow only."""
    arguments = OrderFactsArguments.model_validate(
        {"outTradeNo": out_trade_no}
    )
    return query_facts(
        path="/api/v1/agent/order/refund/preview",
        body={"outTradeNo": arguments.outTradeNo},
        user_id=user_id,
        request_id=request_id,
        data_model=RefundPreviewFacts,
    )
