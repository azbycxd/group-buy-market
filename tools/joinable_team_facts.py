from langchain.tools import ToolRuntime, tool

from tools.arguments import ActivityIdArguments
from tools.context import AgentContext
from tools.facts import JoinableTeamFacts
from tools.facts_client import query_facts


@tool
def get_joinable_team_facts(
    activityId: int,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """查询指定活动当前可加入的团队及活动团队统计事实。"""
    arguments = ActivityIdArguments.model_validate({"activityId": activityId})
    return query_facts(
        path="/api/v1/agent/team/joinable-facts",
        body={"activityId": arguments.activityId},
        user_id=runtime.context.user_id,
        data_model=JoinableTeamFacts,
    )
