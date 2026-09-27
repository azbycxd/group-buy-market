from langchain.tools import ToolRuntime, tool

from tools.arguments import ActivityIdArguments
from tools.context import AgentContext
from tools.facts import UserEligibilityFacts
from tools.facts_client import query_facts


@tool
def get_user_eligibility_facts(
    activityId: int,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """查询当前用户对指定拼团活动的参与资格、次数和门禁事实。"""
    arguments = ActivityIdArguments.model_validate({"activityId": activityId})
    return query_facts(
        path="/api/v1/agent/activity/eligibility-facts",
        body={"activityId": arguments.activityId},
        user_id=runtime.context.user_id,
        data_model=UserEligibilityFacts,
    )
