import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from tools.activity_facts import get_activity_facts
from tools.context import AgentContext
from tools.eligibility_facts import get_user_eligibility_facts
from tools.joinable_team_facts import get_joinable_team_facts
from tools.order_facts import get_order_facts
from tools.rule_search import search_group_buy_rules


load_dotenv(override=True)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def create_order_agent():
    model = ChatOpenAI(
        base_url=_required_env("OPENAI_BASE_URL"),
        model=_required_env("OPENAI_MODEL"),
        api_key=_required_env("OPENAI_API_KEY"),
    )
    return create_agent(
        model=model,
        tools=[
            get_order_facts,
            get_activity_facts,
            get_user_eligibility_facts,
            get_joinable_team_facts,
            search_group_buy_rules,
        ],
        context_schema=AgentContext,
        system_prompt=(
            "你是拼团诊断助手，应根据用户问题调用相关只读工具。"
            "用户询问订单状态时，必须调用 get_order_facts，"
            "当工具返回 code=0000 时，根据 data.order.status 用中文简洁回答；"
            "当工具返回 ORDER_NOT_FOUND_OR_NOT_AUTHORIZED 时，只说明订单不存在或无权限。"
            "不要把业务错误码当作订单状态，也不要猜测订单状态。"
            "业务服务暂时不可用时必须如实说明“暂时无法查询”，禁止猜测业务状态。"
        ),
    )
