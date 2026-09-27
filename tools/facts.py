from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
)


class FactsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OrderFact(FactsModel):
    status: StrictStr


class TeamFacts(FactsModel):
    status: StrictStr
    target_count: StrictInt = Field(alias="targetCount", ge=0)
    lock_count: StrictInt = Field(alias="lockCount", ge=0)
    complete_count: StrictInt = Field(alias="completeCount", ge=0)
    valid_end_time: StrictStr | None = Field(default=None, alias="validEndTime")


class ActivityStatusFact(FactsModel):
    status: StrictStr


class OrderReferences(FactsModel):
    team_id: StrictStr = Field(alias="teamId")
    activity_id: StrictInt = Field(alias="activityId", gt=0)


class OrderFacts(FactsModel):
    order: OrderFact
    team: TeamFacts
    activity: ActivityStatusFact
    references: OrderReferences


class CandidateTeamFacts(FactsModel):
    team_id: StrictStr = Field(alias="teamId")
    target_count: StrictInt = Field(alias="targetCount", ge=0)
    complete_count: StrictInt = Field(alias="completeCount", ge=0)
    lock_count: StrictInt = Field(alias="lockCount", ge=0)
    valid_end_time: StrictStr | None = Field(default=None, alias="validEndTime")


class TeamStatistics(FactsModel):
    all_team_count: StrictInt = Field(alias="allTeamCount", ge=0)
    all_team_complete_count: StrictInt = Field(alias="allTeamCompleteCount", ge=0)
    all_team_user_count: StrictInt = Field(alias="allTeamUserCount", ge=0)


class JoinableTeamFacts(FactsModel):
    activity_id: StrictInt = Field(alias="activityId", gt=0)
    candidate_teams: list[CandidateTeamFacts] = Field(alias="candidateTeams")
    statistics: TeamStatistics


class ActivityDetails(FactsModel):
    activity_id: StrictInt = Field(alias="activityId", gt=0)
    status: StrictStr
    start_time: StrictStr = Field(alias="startTime")
    end_time: StrictStr = Field(alias="endTime")
    tag_scope: StrictStr = Field(alias="tagScope")
    user_take_limit: StrictInt | None = Field(alias="userTakeLimit")
    evaluated_at: StrictStr = Field(alias="evaluatedAt")
    within_valid_time: StrictBool = Field(alias="withinValidTime")


class ActivityFacts(FactsModel):
    activity: ActivityDetails


class UserEligibilityFacts(FactsModel):
    activity_id: StrictInt = Field(alias="activityId", gt=0)
    tag_rule_configured: StrictBool = Field(alias="tagRuleConfigured")
    tag_crowd_data_available: StrictBool = Field(alias="tagCrowdDataAvailable")
    tag_gate_passed: StrictBool = Field(alias="tagGatePassed")
    tag_visibility_allowed: StrictBool = Field(alias="tagVisibilityAllowed")
    tag_participation_allowed: StrictBool = Field(alias="tagParticipationAllowed")
    user_take_count: StrictInt = Field(alias="userTakeCount", ge=0)
    user_take_limit: StrictInt | None = Field(alias="userTakeLimit")
    participation_limit_reached: StrictBool = Field(alias="participationLimitReached")
    market_downgraded: StrictBool = Field(alias="marketDowngraded")
    user_within_release_range: StrictBool = Field(alias="userWithinReleaseRange")


class RefundPreviewFacts(FactsModel):
    order_status: Literal["CREATE", "COMPLETE", "CLOSE"] = Field(
        alias="orderStatus"
    )
    team_status: Literal[
        "PROGRESS",
        "COMPLETE",
        "FAIL",
        "COMPLETE_FAIL",
    ] = Field(alias="teamStatus")
    refund_type: Literal[
        "UNPAID",
        "PAID_UNFORMED",
        "PAID_FORMED",
    ] = Field(alias="refundType")
    refund_proposal_allowed: StrictBool = Field(alias="refundProposalAllowed")
    requires_manual_review: StrictBool = Field(alias="requiresManualReview")
    order_update_time: StrictStr = Field(alias="orderUpdateTime")
    team_update_time: StrictStr = Field(alias="teamUpdateTime")


class RuleMatch(FactsModel):
    rule_id: StrictStr
    title: StrictStr
    content: StrictStr


class RuleSearchFacts(FactsModel):
    query: StrictStr
    matches: list[RuleMatch]


class FactsEnvelope(FactsModel):
    code: StrictStr
    info: StrictStr
    data: Any | None = None


class ToolResult(FactsModel):
    success: StrictBool
    code: StrictStr
    message: StrictStr
    retryable: StrictBool
    data: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
