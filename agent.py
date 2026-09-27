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
            "并根据工具返回的 status 用中文简洁回答；不要猜测订单状态。"
        ),
    )
