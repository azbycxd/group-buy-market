import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from tools.order_facts import AgentContext, get_order_facts


load_dotenv()


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
        tools=[get_order_facts],
        context_schema=AgentContext,
        system_prompt=(
            "你是订单状态助手。用户询问订单状态时，必须调用 get_order_facts，"
            "当工具返回 code=0000 时，根据 data.status 用中文简洁回答；"
            "当工具返回 ORDER_NOT_FOUND_OR_NOT_AUTHORIZED 时，只说明订单不存在或无权限。"
            "不要把业务错误码当作订单状态，也不要猜测订单状态。"
        ),
    )
