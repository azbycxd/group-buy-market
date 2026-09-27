import asyncio
import json
import os
from typing import Annotated, Any

from fastapi import FastAPI, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tools.facts import (
    ActivityFacts,
    FactsEnvelope,
    JoinableTeamFacts,
    OrderFacts,
    RefundExecutionFacts,
    RefundPreviewFacts,
    UserEligibilityFacts,
)


app = FastAPI(title="Fake Java Facts Service")


class OrderFactsRequest(BaseModel):
    outTradeNo: str


class ActivityFactsRequest(BaseModel):
    activityId: int


class RefundExecuteRequest(BaseModel):
    outTradeNo: str
    idempotencyKey: str
    expectedVersion: str
    expectedRefundType: str


UserHeader = Annotated[
    str | None,
    Header(alias="X-Dev-Authenticated-User-Id"),
]


ORDERS: dict[str, dict[str, Any]] = {
    "ORD100001": {
        "owner": "demo-user",
        "data": {
            "order": {"status": "CLOSE"},
            "team": {
                "status": "PROGRESS",
                "targetCount": 3,
                "lockCount": 0,
                "completeCount": 1,
                "validEndTime": "2026-12-31T23:59:59+08:00",
            },
            "activity": {"status": "EFFECTIVE"},
            "references": {"teamId": "18781389", "activityId": 100123},
        },
    },
    "ORD100002": {
        "owner": "user-002",
        "data": {
            "order": {"status": "NORMAL"},
            "team": {
                "status": "PROGRESS",
                "targetCount": 3,
                "lockCount": 1,
                "completeCount": 1,
                "validEndTime": "2026-12-31T23:59:59+08:00",
            },
            "activity": {"status": "EFFECTIVE"},
            "references": {"teamId": "18781390", "activityId": 100123},
        },
    },
    "ORD200001": {
        "owner": "demo-user",
        "data": {
            "order": {"status": "COMPLETE"},
            "team": {
                "status": "PROGRESS",
                "targetCount": 3,
                "lockCount": 0,
                "completeCount": 2,
                "validEndTime": "2026-12-31T23:59:59+08:00",
            },
            "activity": {"status": "EFFECTIVE"},
            "references": {"teamId": "18781401", "activityId": 100123},
        },
    },
    "ORD200002": {
        "owner": "demo-user",
        "data": {
            "order": {"status": "COMPLETE"},
            "team": {
                "status": "COMPLETE",
                "targetCount": 3,
                "lockCount": 0,
                "completeCount": 3,
                "validEndTime": "2026-12-31T23:59:59+08:00",
            },
            "activity": {"status": "EFFECTIVE"},
            "references": {"teamId": "18781402", "activityId": 100123},
        },
    },
}


REFUND_PREVIEWS: dict[str, dict[str, Any]] = {
    "ORD100001": {
        "orderStatus": "CLOSE",
        "teamStatus": "PROGRESS",
        "refundType": "PAID_UNFORMED",
        "refundProposalAllowed": False,
        "requiresManualReview": False,
        "orderUpdateTime": "2026-09-27T09:00:00+08:00",
        "teamUpdateTime": "2026-09-27T09:03:00+08:00",
    },
    "ORD200001": {
        "orderStatus": "COMPLETE",
        "teamStatus": "PROGRESS",
        "refundType": "PAID_UNFORMED",
        "refundProposalAllowed": True,
        "requiresManualReview": False,
        "orderUpdateTime": "2026-09-27T10:00:00+08:00",
        "teamUpdateTime": "2026-09-27T10:05:00+08:00",
    },
    "ORD200002": {
        "orderStatus": "COMPLETE",
        "teamStatus": "COMPLETE",
        "refundType": "PAID_FORMED",
        "refundProposalAllowed": True,
        "requiresManualReview": True,
        "orderUpdateTime": "2026-09-27T11:00:00+08:00",
        "teamUpdateTime": "2026-09-27T11:07:00+08:00",
    },
}


REFUND_EXECUTIONS: dict[str, dict[str, Any]] = {}


ACTIVITIES: dict[int, dict[str, Any]] = {
    100123: {
        "activity": {
            "activityId": 100123,
            "status": "EFFECTIVE",
            "startTime": "2026-01-01T00:00:00+08:00",
            "endTime": "2027-12-31T23:59:59+08:00",
            "tagScope": "2",
            "userTakeLimit": 1,
            "evaluatedAt": "2026-09-27T12:00:00+08:00",
            "withinValidTime": True,
        }
    },
    100124: {
        "activity": {
            "activityId": 100124,
            "status": "OVERDUE",
            "startTime": "2025-01-01T00:00:00+08:00",
            "endTime": "2025-12-31T23:59:59+08:00",
            "tagScope": "1,2",
            "userTakeLimit": 1,
            "evaluatedAt": "2026-09-27T12:00:00+08:00",
            "withinValidTime": False,
        }
    },
    100125: {
        "activity": {
            "activityId": 100125,
            "status": "EFFECTIVE",
            "startTime": "2026-01-01T00:00:00+08:00",
            "endTime": "2027-12-31T23:59:59+08:00",
            "tagScope": "2",
            "userTakeLimit": 1,
            "evaluatedAt": "2026-09-27T12:00:00+08:00",
            "withinValidTime": True,
        }
    },
}


ELIGIBILITY: dict[tuple[str, int], dict[str, Any]] = {
    ("demo-user", 100123): {
        "activityId": 100123,
        "tagRuleConfigured": True,
        "tagCrowdDataAvailable": True,
        "tagGatePassed": True,
        "tagVisibilityAllowed": True,
        "tagParticipationAllowed": True,
        "userTakeCount": 0,
        "userTakeLimit": 1,
        "participationLimitReached": False,
        "marketDowngraded": False,
        "userWithinReleaseRange": True,
    },
    ("demo-user", 100124): {
        "activityId": 100124,
        "tagRuleConfigured": True,
        "tagCrowdDataAvailable": True,
        "tagGatePassed": False,
        "tagVisibilityAllowed": True,
        "tagParticipationAllowed": False,
        "userTakeCount": 0,
        "userTakeLimit": 1,
        "participationLimitReached": False,
        "marketDowngraded": False,
        "userWithinReleaseRange": True,
    },
    ("demo-user", 100125): {
        "activityId": 100125,
        "tagRuleConfigured": True,
        "tagCrowdDataAvailable": True,
        "tagGatePassed": True,
        "tagVisibilityAllowed": True,
        "tagParticipationAllowed": True,
        "userTakeCount": 1,
        "userTakeLimit": 1,
        "participationLimitReached": True,
        "marketDowngraded": False,
        "userWithinReleaseRange": True,
    },
}


JOINABLE_TEAMS: dict[int, dict[str, Any]] = {
    100123: {
        "activityId": 100123,
        "candidateTeams": [
            {
                "teamId": "18781391",
                "targetCount": 3,
                "completeCount": 1,
                "lockCount": 0,
                "validEndTime": "2026-12-31T23:59:59+08:00",
            }
        ],
        "statistics": {
            "allTeamCount": 2,
            "allTeamCompleteCount": 1,
            "allTeamUserCount": 4,
        },
    },
    100124: {
        "activityId": 100124,
        "candidateTeams": [],
        "statistics": {
            "allTeamCount": 2,
            "allTeamCompleteCount": 2,
            "allTeamUserCount": 6,
        },
    },
    100125: {
        "activityId": 100125,
        "candidateTeams": [],
        "statistics": {
            "allTeamCount": 0,
            "allTeamCompleteCount": 0,
            "allTeamUserCount": 0,
        },
    },
}


def success(data: BaseModel) -> dict[str, Any]:
    return FactsEnvelope(
        code="0000",
        info="成功",
        data=data.model_dump(mode="json", by_alias=True),
    ).model_dump(mode="json")


def business_error(code: str, info: str) -> dict[str, Any]:
    return FactsEnvelope(code=code, info=info, data=None).model_dump(mode="json")


def auth_error(user_id: str | None) -> dict[str, Any] | None:
    if user_id is None:
        return business_error("AUTH_REQUIRED", "缺少可信用户身份")
    return None


def configured_delay_seconds() -> float:
    try:
        delay = float(os.getenv("FAKE_JAVA_DELAY_SECONDS", "0"))
    except ValueError:
        delay = 0
    return max(delay, 0)


def delayed_response(payload: dict[str, Any]) -> dict[str, Any] | StreamingResponse:
    delay = configured_delay_seconds()
    if delay == 0:
        return payload

    async def body() -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            # Keep the test connection active so the request-level timeout,
            # rather than httpx's per-read timeout, is what ends the request.
            yield b" "
            await asyncio.sleep(min(1.0, remaining))
        yield json.dumps(payload, ensure_ascii=False).encode("utf-8")

    return StreamingResponse(body(), media_type="application/json")


@app.post("/api/v1/agent/order/facts")
async def get_order_facts(
    request: OrderFactsRequest,
    user_id: UserHeader = None,
) -> Any:
    order = ORDERS.get(request.outTradeNo)
    if order is None or order["owner"] != user_id:
        return delayed_response(
            business_error(
                "ORDER_NOT_FOUND_OR_NOT_AUTHORIZED",
                "订单不存在或无权限",
            )
        )
    return delayed_response(success(OrderFacts.model_validate(order["data"])))


@app.post("/api/v1/agent/order/refund/preview")
async def get_refund_preview(
    request: OrderFactsRequest,
    user_id: UserHeader = None,
) -> Any:
    order = ORDERS.get(request.outTradeNo)
    preview = REFUND_PREVIEWS.get(request.outTradeNo)
    if order is None or preview is None or order["owner"] != user_id:
        return delayed_response(
            business_error(
                "ORDER_NOT_FOUND_OR_NOT_AUTHORIZED",
                "订单不存在或无权限",
            )
        )
    return delayed_response(
        success(RefundPreviewFacts.model_validate(preview))
    )


@app.post("/api/v1/agent/order/refund")
async def execute_refund(
    request: RefundExecuteRequest,
    user_id: UserHeader = None,
) -> Any:
    order = ORDERS.get(request.outTradeNo)
    preview = REFUND_PREVIEWS.get(request.outTradeNo)
    if order is None or preview is None or order["owner"] != user_id:
        return delayed_response(
            business_error(
                "ORDER_NOT_FOUND_OR_NOT_AUTHORIZED",
                "订单不存在或无权限",
            )
        )

    previous = REFUND_EXECUTIONS.get(request.idempotencyKey)
    if previous is not None:
        replay = {**previous, "idempotentReplay": True}
        return delayed_response(
            success(RefundExecutionFacts.model_validate(replay))
        )

    expected_version = (
        f"{preview['orderUpdateTime']}|{preview['teamUpdateTime']}"
    )
    if (
        request.expectedVersion != expected_version
        or request.expectedRefundType != preview["refundType"]
    ):
        result = {
            "status": "FAILED",
            "resultCode": "VERSION_CHANGED",
            "refundExecuted": False,
            "idempotentReplay": False,
        }
    elif request.expectedRefundType == "PAID_FORMED":
        result = {
            "status": "FAILED",
            "resultCode": "MANUAL_REVIEW_REQUIRED",
            "refundExecuted": False,
            "idempotentReplay": False,
        }
    elif order["data"]["order"]["status"] == "CLOSE":
        result = {
            "status": "SUCCEEDED",
            "resultCode": "ALREADY_CLOSED",
            "refundExecuted": False,
            "idempotentReplay": False,
        }
    else:
        order["data"]["order"]["status"] = "CLOSE"
        preview["orderStatus"] = "CLOSE"
        result = {
            "status": "SUCCEEDED",
            "resultCode": "REFUND_ACCEPTED",
            "refundExecuted": True,
            "idempotentReplay": False,
        }
    REFUND_EXECUTIONS[request.idempotencyKey] = result
    return delayed_response(
        success(RefundExecutionFacts.model_validate(result))
    )


@app.post("/api/v1/agent/activity/facts")
async def get_activity_facts(
    request: ActivityFactsRequest,
    user_id: UserHeader = None,
) -> Any:
    if error := auth_error(user_id):
        return delayed_response(error)
    data = ACTIVITIES.get(request.activityId)
    if data is None:
        return delayed_response(business_error("ACTIVITY_NOT_FOUND", "活动不存在"))
    return delayed_response(success(ActivityFacts.model_validate(data)))


@app.post("/api/v1/agent/activity/eligibility-facts")
async def get_user_eligibility_facts(
    request: ActivityFactsRequest,
    user_id: UserHeader = None,
) -> Any:
    if error := auth_error(user_id):
        return delayed_response(error)
    if request.activityId not in ACTIVITIES:
        return delayed_response(business_error("ACTIVITY_NOT_FOUND", "活动不存在"))
    data = ELIGIBILITY.get((user_id, request.activityId))
    if data is None:
        data = {
            "activityId": request.activityId,
            "tagRuleConfigured": True,
            "tagCrowdDataAvailable": True,
            "tagGatePassed": False,
            "tagVisibilityAllowed": True,
            "tagParticipationAllowed": False,
            "userTakeCount": 0,
            "userTakeLimit": 1,
            "participationLimitReached": False,
            "marketDowngraded": False,
            "userWithinReleaseRange": True,
        }
    return delayed_response(success(UserEligibilityFacts.model_validate(data)))


@app.post("/api/v1/agent/team/joinable-facts")
async def get_joinable_team_facts(
    request: ActivityFactsRequest,
    user_id: UserHeader = None,
) -> Any:
    if error := auth_error(user_id):
        return delayed_response(error)
    data = JOINABLE_TEAMS.get(request.activityId)
    if data is None:
        return delayed_response(business_error("ACTIVITY_NOT_FOUND", "活动不存在"))
    return delayed_response(success(JoinableTeamFacts.model_validate(data)))
