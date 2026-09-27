from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, TypeVar

from understanding import EntityType, InformationNeed, ParsedEntity


class RequirementStatus(StrEnum):
    ANSWERABLE = "ANSWERABLE"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
    NEED_USER_INPUT = "NEED_USER_INPUT"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class Capability:
    supported: bool
    required_entities: tuple[EntityType, ...]
    required_evidence: tuple[str, ...]
    unsupported_reason: str | None = None


CAPABILITY_TABLE: dict[InformationNeed, Capability] = {
    InformationNeed.ORDER_STATUS: Capability(
        supported=True,
        required_entities=(EntityType.ORDER,),
        required_evidence=("get_order_facts",),
    ),
    InformationNeed.ACTIVITY_VALIDITY: Capability(
        supported=True,
        required_entities=(EntityType.ACTIVITY,),
        required_evidence=("get_activity_facts",),
    ),
    InformationNeed.USER_ELIGIBILITY: Capability(
        supported=True,
        required_entities=(EntityType.ACTIVITY,),
        required_evidence=("get_user_eligibility_facts",),
    ),
    InformationNeed.JOINABLE_TEAMS: Capability(
        supported=True,
        required_entities=(EntityType.ACTIVITY,),
        required_evidence=("get_joinable_team_facts",),
    ),
    InformationNeed.RULE_EXPLANATION: Capability(
        supported=True,
        required_entities=(),
        required_evidence=("search_group_buy_rules",),
    ),
    InformationNeed.REFUND_ARRIVAL: Capability(
        supported=False,
        required_entities=(EntityType.ORDER,),
        required_evidence=(),
        unsupported_reason=(
            "系统只能看到拼团订单状态，无法确认支付渠道资金到账情况。"
        ),
    ),
    InformationNeed.REFUND_REQUEST: Capability(
        supported=True,
        required_entities=(),
        required_evidence=("search_group_buy_rules",),
    ),
    InformationNeed.OUT_OF_SCOPE: Capability(
        supported=False,
        required_entities=(),
        required_evidence=(),
        unsupported_reason="当前系统只支持拼团只读查询，无法执行该请求。",
    ),
}


@dataclass(frozen=True)
class RequirementDecision:
    status: RequirementStatus
    unsupported_needs: tuple[InformationNeed, ...] = ()
    missing_entities: tuple[EntityType, ...] = ()
    missing_evidence: tuple[str, ...] = ()


T = TypeVar("T")


def _unique(values: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(values))


class RequirementTracker:
    """Evaluate capability requirements without model calls or heuristics."""

    def evaluate(
        self,
        needs: list[InformationNeed],
        entities: list[ParsedEntity],
        evidence: Iterable[str],
    ) -> RequirementDecision:
        unsupported_needs = _unique(
            need for need in needs if not CAPABILITY_TABLE[need].supported
        )
        if unsupported_needs:
            return RequirementDecision(
                status=RequirementStatus.UNSUPPORTED,
                unsupported_needs=unsupported_needs,
            )

        available_entities = {entity.entity_type for entity in entities}
        missing_entities = _unique(
            entity_type
            for need in needs
            for entity_type in CAPABILITY_TABLE[need].required_entities
            if entity_type not in available_entities
        )
        if missing_entities:
            return RequirementDecision(
                status=RequirementStatus.NEED_USER_INPUT,
                missing_entities=missing_entities,
            )

        available_evidence = set(evidence)
        missing_evidence = _unique(
            evidence_name
            for need in needs
            for evidence_name in CAPABILITY_TABLE[need].required_evidence
            if evidence_name not in available_evidence
        )
        if missing_evidence:
            return RequirementDecision(
                status=RequirementStatus.NEED_MORE_EVIDENCE,
                missing_evidence=missing_evidence,
            )

        return RequirementDecision(status=RequirementStatus.ANSWERABLE)
