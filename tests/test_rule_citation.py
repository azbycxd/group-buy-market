from __future__ import annotations

from agent import _claim_errors, _validated_answer_outcome
from evidence import CLEAR_EVIDENCE, Evidence, EvidenceType, merge_evidence
from outcome import AgentOutcome, Claim, ClaimType, OutcomeKind


def _rule_evidence(
    rule_id: str,
    *,
    index: int = 0,
    title: str | None = None,
) -> Evidence:
    return Evidence(
        path=f"search_group_buy_rules.matches.{index}",
        type=EvidenceType.RULE,
        value={
            "rule_id": rule_id,
            "title": title or f"真实标题 {rule_id}",
            "content": "真实规则内容",
            "rerank_score": 0.99,
        },
    )


def _outcome(
    rule_id: str | None,
    *,
    evidence_path: str = "search_group_buy_rules.matches.0",
    text: str = "规则结论正文。",
) -> AgentOutcome:
    return AgentOutcome(
        kind=OutcomeKind.ANSWER,
        claims=[
            Claim(
                text=text,
                type=ClaimType.RULE,
                evidence=[evidence_path],
                rule_id=rule_id,
            )
        ],
        final_answer="模型自由文本不应直接展示",
    )


def test_nonexistent_rule_id_fails_validation() -> None:
    evidence = [_rule_evidence("PRE-004")]

    errors = _claim_errors(_outcome("NOT-FOUND"), evidence)

    assert any("不在本轮检索结果" in error for error in errors)


def test_real_rule_id_not_returned_this_round_fails_validation() -> None:
    evidence = [_rule_evidence("ORD-004")]

    errors = _claim_errors(_outcome("PRE-004"), evidence)

    assert any("不在本轮检索结果" in error for error in errors)


def test_internal_rerank_candidate_outside_final_pool_fails_validation() -> None:
    # A rule scored internally in Rerank Top10 is not citeable unless it was
    # included in the final Tool matches/Evidence candidate pool.
    final_pool_evidence = [_rule_evidence("ORD-001")]

    errors = _claim_errors(_outcome("ORD-005"), final_pool_evidence)

    assert any("不在本轮检索结果" in error for error in errors)


def test_rule_id_from_previous_task_fails_after_task_evidence_clear() -> None:
    previous = [_rule_evidence("PRE-004")]
    current = merge_evidence(previous, CLEAR_EVIDENCE)

    errors = _claim_errors(_outcome("PRE-004"), current)

    assert current == []
    assert any("不存在的 Evidence" in error for error in errors)
    assert any("不在本轮检索结果" in error for error in errors)


def test_rule_id_in_current_match_and_corresponding_path_passes() -> None:
    evidence = [_rule_evidence("PRE-004")]

    assert _claim_errors(_outcome("PRE-004"), evidence) == []


def test_visible_title_is_rebuilt_from_current_evidence() -> None:
    evidence = [
        _rule_evidence(
            "PRE-004",
            title="已支付未成团退款可直接处理",
        )
    ]
    generated = _outcome(
        "PRE-004",
        text="依据《模型伪造标题》：已支付但未成团时可以直接处理退款。",
    )

    validated = _validated_answer_outcome(generated, evidence)

    assert validated.kind is OutcomeKind.ANSWER
    assert validated.claims[0].rule_id == "PRE-004"
    assert validated.claims[0].text.startswith(
        "依据《已支付未成团退款可直接处理》："
    )
    assert "模型伪造标题" not in validated.final_answer
    assert validated.final_answer == validated.claims[0].text


def test_rule_claim_missing_rule_id_fails_validation() -> None:
    errors = _claim_errors(_outcome(None), [_rule_evidence("PRE-004")])

    assert "RULE Claim 缺少 rule_id" in errors

