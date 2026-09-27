from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI(title="Fake Java Order Service")


class OrderFactsRequest(BaseModel):
    outTradeNo: str


@app.post("/api/v1/agent/order/facts")
def get_order_facts(request: OrderFactsRequest) -> dict[str, str]:
    status_by_order = {
        "ORD100001": "CLOSE",
        "ORD100002": "NORMAL",
    }
    return {
        "outTradeNo": request.outTradeNo,
        "status": status_by_order.get(request.outTradeNo, "NOT_FOUND"),
    }
