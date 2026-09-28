from __future__ import annotations

import pytest

from understanding import (
    EntityType,
    InformationNeed,
    ParsedEntity,
    Understanding,
    normalize_rule_question,
)


def _understanding(*needs: InformationNeed) -> Understanding:
    return Understanding(needs=list(needs), entities=[])


def _order(value: str = "930000000001") -> ParsedEntity:
    return ParsedEntity(
        entity_type=EntityType.ORDER,
        source_text=value,
        field_name="outTradeNo",
        value=value,
    )


def test_explicit_refund_request_is_not_swallowed_by_rule_normalization() -> None:
    raw = _understanding(InformationNeed.REFUND_REQUEST)

    normalized = normalize_rule_question(
        "帮我退掉订单930000000001",
        raw,
        [_order()],
    )

    assert normalized.needs == [InformationNeed.REFUND_REQUEST]


@pytest.mark.parametrize(
    ("text", "raw_needs", "expected"),
    [
        (
            "钱付了但没成团，能退吗",
            [InformationNeed.REFUND_REQUEST, InformationNeed.JOINABLE_TEAMS],
            InformationNeed.REFUND_POLICY,
        ),
        (
            "已关闭还能重新付款吗",
            [InformationNeed.ORDER_STATUS],
            InformationNeed.RULE_EXPLANATION,
        ),
        (
            "待支付、已完成、已关闭是什么意思",
            [InformationNeed.ORDER_STATUS, InformationNeed.RULE_EXPLANATION],
            InformationNeed.RULE_EXPLANATION,
        ),
        (
            "已成团退款是不是要人工审核",
            [InformationNeed.REFUND_REQUEST],
            InformationNeed.REFUND_POLICY,
        ),
    ],
)
def test_generic_business_rule_questions_are_normalized(
    text: str,
    raw_needs: list[InformationNeed],
    expected: InformationNeed,
) -> None:
    normalized = normalize_rule_question(
        text,
        Understanding(needs=raw_needs, entities=[]),
        [],
    )

    assert normalized.needs == [expected]


def test_concrete_order_status_query_keeps_fact_need() -> None:
    raw = _understanding(InformationNeed.ORDER_STATUS)

    normalized = normalize_rule_question(
        "订单930000000001现在什么状态",
        raw,
        [_order()],
    )

    assert normalized.needs == [InformationNeed.ORDER_STATUS]


def test_missing_activity_fact_question_is_not_retyped_as_rule() -> None:
    raw = _understanding(InformationNeed.ACTIVITY_VALIDITY)

    normalized = normalize_rule_question("这个活动还有效吗", raw, [])

    assert normalized.needs == [InformationNeed.ACTIVITY_VALIDITY]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "钱已经付了，但是这个团人还没凑齐，我现在想退的话能直接处理吗？",
            InformationNeed.REFUND_POLICY,
        ),
        (
            "这单已经关掉了，我现在再补钱还能把它弄回来吗？",
            InformationNeed.RULE_EXPLANATION,
        ),
        (
            "订单上的待支付、已完成、已关闭分别是什么意思？",
            InformationNeed.RULE_EXPLANATION,
        ),
        (
            "订单都已经关闭了，还能再生成一次退款申请吗？",
            InformationNeed.REFUND_POLICY,
        ),
    ],
)
def test_d3b_blocking_questions_normalize_to_rule_need(
    text: str,
    expected: InformationNeed,
) -> None:
    raw = _understanding(
        InformationNeed.ORDER_STATUS,
        InformationNeed.REFUND_REQUEST,
        InformationNeed.OUT_OF_SCOPE,
    )

    normalized = normalize_rule_question(text, raw, [])

    assert normalized.needs == [expected]
