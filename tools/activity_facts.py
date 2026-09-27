from langchain.tools import ToolRuntime, tool

from tools.arguments import ActivityIdArguments
from tools.context import AgentContext
from tools.facts import ActivityFacts
from tools.facts_client import query_facts


@tool
def get_activity_facts(
    activityId: int,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """查询拼团活动状态、有效时间和参与次数上限等事实。"""
    arguments = ActivityIdArguments.model_validate({"activityId": activityId})
    return query_facts(
        path="/api/v1/agent/activity/facts",
        body={"activityId": arguments.activityId},
        user_id=runtime.context.user_id,
        request_id=runtime.context.request_id,
        data_model=ActivityFacts,
    )
