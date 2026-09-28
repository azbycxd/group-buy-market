from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from fake_java.main import (
    ORDERS,
    REFUND_EXECUTIONS,
    REFUND_EXECUTION_OWNERS,
    app,
)
from tools.refund_execute import ExecutionCertainty
from tools.refund_result import query_refund_result


ACTION = {
    "user_id": "demo-user",
    "idempotency_key": "refund-c4-unit",
}


def _response(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": "0000", "info": "成功", "data": data},
    )


class RefundResultClientTests(unittest.TestCase):
    def test_status_mapping(self) -> None:
        cases = (
            ("SUCCEEDED", ExecutionCertainty.SUCCESS, True),
            ("FAILED", ExecutionCertainty.FAILURE, False),
            ("ABANDONED", ExecutionCertainty.FAILURE, False),
            ("PROCESSING", ExecutionCertainty.UNKNOWN, False),
        )
        for status, certainty, executed in cases:
            with (
                self.subTest(status=status),
                patch(
                    "tools.refund_result._request_target",
                    return_value=("http://java", {}, "real"),
                ),
                patch.object(
                    __import__(
                        "tools.refund_result",
                        fromlist=["REFUND_RESULT_HTTP_CLIENT"],
                    ).REFUND_RESULT_HTTP_CLIENT,
                    "post",
                    return_value=_response(
                        {
                            "status": status,
                            "resultCode": f"RESULT_{status}",
                            "refundExecuted": executed,
                        }
                    ),
                ),
            ):
                result = query_refund_result(ACTION)
            self.assertEqual(result.certainty, certainty)
            self.assertEqual(result.status, status)
            self.assertEqual(result.refund_executed, executed)

    def test_timeout_and_connection_error_are_unknown(self) -> None:
        for error, code in (
            (httpx.ReadTimeout("timeout"), "RESULT_QUERY_TIMEOUT"),
            (
                httpx.ConnectError("connection"),
                "RESULT_QUERY_CONNECTION_ERROR",
            ),
        ):
            with (
                self.subTest(code=code),
                patch(
                    "tools.refund_result._request_target",
                    return_value=("http://java", {}, "real"),
                ),
                patch(
                    "tools.refund_result.REFUND_RESULT_HTTP_CLIENT.post",
                    side_effect=error,
                ),
            ):
                result = query_refund_result(ACTION)
            self.assertEqual(result.certainty, ExecutionCertainty.UNKNOWN)
            self.assertEqual(result.result_code, code)


class FakeJavaRefundResultTests(unittest.TestCase):
    def setUp(self) -> None:
        REFUND_EXECUTIONS.clear()
        REFUND_EXECUTION_OWNERS.clear()
        ORDERS["ORD200001"]["data"]["order"]["status"] = "COMPLETE"
        self.client = TestClient(app)
        self.headers = {"X-Dev-Authenticated-User-Id": "demo-user"}

    def test_result_reserves_missing_key_and_late_refund_does_not_execute(
        self,
    ) -> None:
        key = "refund-c4-abandoned"
        result_response = self.client.post(
            "/api/v1/agent/order/refund/result",
            json={"idempotencyKey": key},
            headers=self.headers,
        )
        self.assertEqual(result_response.status_code, 200)
        self.assertEqual(result_response.json()["data"]["status"], "ABANDONED")
        self.assertEqual(
            result_response.json()["data"]["resultCode"],
            "NOT_RECEIVED_BEFORE_QUERY",
        )

        refund_response = self.client.post(
            "/api/v1/agent/order/refund",
            json={
                "outTradeNo": "ORD200001",
                "idempotencyKey": key,
                "expectedVersion": (
                    "2026-09-27T10:00:00+08:00|"
                    "2026-09-27T10:05:00+08:00"
                ),
                "expectedRefundType": "PAID_UNFORMED",
            },
            headers=self.headers,
        )
        self.assertEqual(refund_response.status_code, 200)
        self.assertEqual(refund_response.json()["data"]["status"], "FAILED")
        self.assertEqual(
            refund_response.json()["data"]["resultCode"],
            "ABANDONED",
        )
        self.assertFalse(refund_response.json()["data"]["refundExecuted"])
        self.assertEqual(
            ORDERS["ORD200001"]["data"]["order"]["status"],
            "COMPLETE",
        )


if __name__ == "__main__":
    unittest.main()
