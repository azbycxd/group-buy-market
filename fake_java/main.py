from typing import Annotated

from fastapi import FastAPI, Header
from pydantic import BaseModel


app = FastAPI(title="Fake Java Order Service")


class OrderFactsRequest(BaseModel):
    outTradeNo: str


ORDERS = {
    "ORD100001": {"owner": "demo-user", "status": "CLOSE"},
    "ORD100002": {"owner": "user-002", "status": "NORMAL"},
}


@app.post("/api/v1/agent/order/facts")
def get_order_facts(
    request: OrderFactsRequest,
    user_id: Annotated[
        str | None,
        Header(alias="X-Dev-Authenticated-User-Id"),
    ] = None,
) -> dict[str, object]:
    order = ORDERS.get(request.outTradeNo)
    if order is None or order["owner"] != user_id:
        return {
            "code": "ORDER_NOT_FOUND_OR_NOT_AUTHORIZED",
            "info": "订单不存在或无权限",
            "data": None,
        }

    return {
        "code": "0000",
        "info": "成功",
        "data": {
            "outTradeNo": request.outTradeNo,
            "status": order["status"],
        },
    }
