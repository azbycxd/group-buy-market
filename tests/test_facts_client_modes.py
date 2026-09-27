from __future__ import annotations

import time
import unittest
from unittest.mock import Mock, patch

import jwt

from tools.facts import OrderFacts
from tools.facts_client import query_facts


ORDER_ENVELOPE = {
    "code": "0000",
    "info": "success",
    "data": {
        "order": {"status": "CLOSE"},
        "team": {
            "status": "COMPLETE",
            "targetCount": 2,
            "lockCount": 2,
            "completeCount": 2,
            "validEndTime": "2026-09-27T12:00:00",
        },
        "activity": {"status": "EFFECTIVE"},
        "references": {"teamId": "18781389", "activityId": 100123},
    },
}


def _response() -> Mock:
    response = Mock()
    response.status_code = 200
    response.json.return_value = ORDER_ENVELOPE
    return response


class FactsClientModeTests(unittest.TestCase):
    @patch("tools.facts_client.FACTS_HTTP_CLIENT.post")
    def test_fake_mode_uses_only_development_identity_header(
        self,
        post: Mock,
    ) -> None:
        post.return_value = _response()
        environment = {
            "JAVA_BASE_URL": "",
            "FAKE_JAVA_BASE_URL": "http://127.0.0.1:8000",
        }
        with patch.dict("os.environ", environment, clear=False):
            result = query_facts(
                path="/api/v1/agent/order/facts",
                body={"outTradeNo": "ORD100001"},
                user_id="user-001",
                request_id="request-fake",
                data_model=OrderFacts,
            )

        self.assertTrue(result["success"])
        call = post.call_args
        self.assertEqual(call.kwargs["headers"], {
            "X-Dev-Authenticated-User-Id": "user-001",
            "X-Request-Id": "request-fake",
        })
        self.assertNotIn("Authorization", call.kwargs["headers"])
        self.assertNotIn("userId", call.kwargs["json"])

    @patch("tools.facts_client.FACTS_HTTP_CLIENT.post")
    def test_real_mode_uses_internal_jwt_and_trusted_identity_headers(
        self,
        post: Mock,
    ) -> None:
        post.return_value = _response()
        environment = {
            "JAVA_BASE_URL": "http://127.0.0.1:8091",
            "JAVA_INTERNAL_JWT_SECRET": "unit-test-secret",
            "JAVA_INTERNAL_JWT_ISSUER": "group-buy-agent",
            "JAVA_INTERNAL_JWT_AUDIENCE": "group-buy-market",
        }
        with patch.dict("os.environ", environment, clear=False):
            result = query_facts(
                path="/api/v1/agent/order/facts",
                body={"outTradeNo": "644398015396"},
                user_id="xfg05",
                request_id="request-real",
                data_model=OrderFacts,
            )

        self.assertTrue(result["success"])
        call = post.call_args
        headers = call.kwargs["headers"]
        self.assertEqual(headers["X-Authenticated-User-Id"], "xfg05")
        self.assertEqual(headers["X-Request-Id"], "request-real")
        self.assertNotIn("X-Dev-Authenticated-User-Id", headers)
        self.assertNotIn("userId", call.kwargs["json"])
        token = headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token,
            environment["JAVA_INTERNAL_JWT_SECRET"],
            algorithms=["HS256"],
            issuer=environment["JAVA_INTERNAL_JWT_ISSUER"],
            audience=environment["JAVA_INTERNAL_JWT_AUDIENCE"],
        )
        self.assertLessEqual(claims["exp"] - int(time.time()), 60)

    @patch("tools.facts_client.FACTS_HTTP_CLIENT.post")
    def test_real_mode_without_internal_jwt_config_does_not_call_java(
        self,
        post: Mock,
    ) -> None:
        environment = {
            "JAVA_BASE_URL": "http://127.0.0.1:8091",
            "JAVA_INTERNAL_JWT_SECRET": "",
            "JAVA_INTERNAL_JWT_ISSUER": "",
            "JAVA_INTERNAL_JWT_AUDIENCE": "",
        }
        with patch.dict("os.environ", environment, clear=False):
            result = query_facts(
                path="/api/v1/agent/order/facts",
                body={"outTradeNo": "644398015396"},
                user_id="xfg05",
                request_id="request-missing-config",
                data_model=OrderFacts,
            )

        self.assertEqual(result["code"], "SERVICE_UNAVAILABLE")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
