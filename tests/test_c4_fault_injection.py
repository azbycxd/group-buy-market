from __future__ import annotations

import unittest


FAULT_TEST_IDS = (
    # Java 已执行但响应丢失。
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_7_lost_response_reconciles_success_without_second_write",
    # 请求未送达，result 抢先建立 ABANDONED。
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_6_delayed_request_is_abandoned_without_refund",
    # EXECUTING 时 kill Agent 的两种结果。
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_9_kill_after_java_execution_reconciles_success",
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_10_kill_before_java_receives_refund_abandons",
    # 并发确认及跨 session 同订单。
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_4_two_tabs_execute_only_once",
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_8_two_sessions_same_order_have_one_success",
    # Java 暂时不可用，恢复后只读对账。
    "tests.test_c3_agent_dev.C3AgentDevIntegrationTests."
    "test_11_java_unavailable_then_result_reconcile_after_recovery",
    # 连续未知转人工且停止查询。
    "tests.test_c4_reconciler.RefundReconcilerTests."
    "test_processing_reaches_manual_and_is_not_queried_again",
    # 聊天伪造凭证、过期凭证、跨用户凭证。
    "tests.test_c2.C2ConfirmationIntegrationTests."
    "test_13_forged_credential_in_chat_does_not_confirm",
    "tests.test_c2.C2ConfirmationIntegrationTests."
    "test_3_expired_credential_is_rejected",
    "tests.test_c2.C2ConfirmationIntegrationTests."
    "test_5_cross_user_confirmation_is_rejected",
)


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    del standard_tests, pattern
    suite = unittest.TestSuite()
    for test_id in FAULT_TEST_IDS:
        suite.addTests(loader.loadTestsFromName(test_id))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
