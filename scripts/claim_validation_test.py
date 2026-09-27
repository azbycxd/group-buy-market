from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import _validated_answer_outcome
from evidence import Evidence, EvidenceType
from outcome import AgentOutcome, Claim, ClaimType, OutcomeKind


def main() -> None:
    original_draft = "订单退款已经到账。"
    generated = AgentOutcome(
        kind=OutcomeKind.ANSWER,
        claims=[
            Claim(
                text=original_draft,
                type=ClaimType.FACT,
                evidence=["get_order_facts.order.refundArrived"],
            )
        ],
        final_answer=original_draft,
    )
    evidence = [
        Evidence(
            path="get_order_facts.order.status",
            type=EvidenceType.FACT,
            value="CLOSE",
        )
    ]

    outcome = _validated_answer_outcome(generated, evidence)
    assert outcome.kind is OutcomeKind.HANDOFF
    assert original_draft not in outcome.final_answer
    assert outcome.final_answer == (
        "回答中的结论无法与查询到的事实对应，已转人工客服核实。"
    )

    print(f"FAKE_EVIDENCE_PATH: {outcome.kind.value}")
    print(f"DRAFT_LEAKED: {original_draft in outcome.final_answer}")
    print(f"FINAL_ANSWER: {outcome.final_answer}")


if __name__ == "__main__":
    main()
