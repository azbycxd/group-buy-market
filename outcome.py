from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class OutcomeKind(StrEnum):
    ANSWER = "ANSWER"
    REQUEST_INPUT = "REQUEST_INPUT"
    HANDOFF = "HANDOFF"


class ClaimType(StrEnum):
    FACT = "FACT"
    RULE = "RULE"
    CAPABILITY = "CAPABILITY"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: StrictStr
    type: ClaimType
    evidence: list[StrictStr] = Field(default_factory=list)
    rule_id: StrictStr | None = Field(
        default=None,
        description="RULE Claim 必须填写当前规则检索结果中的 rule_id",
    )


class AgentOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OutcomeKind
    claims: list[Claim] = Field(min_length=1)
    final_answer: StrictStr
