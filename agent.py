import os
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import AnyMessage, AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from tools.activity_facts import get_activity_facts
from tools.context import AgentContext
from tools.eligibility_facts import get_user_eligibility_facts
from tools.joinable_team_facts import get_joinable_team_facts
from tools.order_facts import get_order_facts
from tools.rule_search import search_group_buy_rules
from understanding import (
    InformationNeed,
    ParsedEntity,
    Understanding,
    create_understander,
    understand_text,
)


load_dotenv(override=True)


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


SYSTEM_PROMPT = (
    "你是拼团诊断助手，应根据用户问题调用相关只读工具。"
    "用户询问订单状态时，必须调用 get_order_facts，"
    "当工具返回 code=0000 时，根据 data.order.status 用中文简洁回答；"
    "当工具返回 ORDER_NOT_FOUND_OR_NOT_AUTHORIZED 时，只说明订单不存在或无权限。"
    "不要把业务错误码当作订单状态，也不要猜测订单状态。"
    "业务服务暂时不可用时必须如实说明“暂时无法查询”，禁止猜测业务状态。"
)


TOOLS_BY_NEED = {
    InformationNeed.ORDER_STATUS: (get_order_facts,),
    InformationNeed.ACTIVITY_VALIDITY: (get_activity_facts,),
    InformationNeed.USER_ELIGIBILITY: (get_user_eligibility_facts,),
    InformationNeed.JOINABLE_TEAMS: (get_joinable_team_facts,),
    InformationNeed.RULE_EXPLANATION: (search_group_buy_rules,),
    InformationNeed.REFUND_ARRIVAL: (get_order_facts,),
    InformationNeed.REFUND_REQUEST: (search_group_buy_rules,),
}

TOOL_ORDER = (
    get_order_facts,
    get_activity_facts,
    get_user_eligibility_facts,
    get_joinable_team_facts,
    search_group_buy_rules,
)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    understanding: Understanding
    parsed_entities: list[ParsedEntity]


def _create_model() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=_required_env("OPENAI_BASE_URL"),
        model=_required_env("OPENAI_MODEL"),
        api_key=_required_env("OPENAI_API_KEY"),
    )


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    raise ValueError("缺少用户消息")


def _select_tools(needs: list[InformationNeed]) -> list[object]:
    selected_names = {
        tool.name
        for need in needs
        for tool in TOOLS_BY_NEED.get(need, ())
    }
    return [tool for tool in TOOL_ORDER if tool.name in selected_names]


def _entity_context(entities: list[ParsedEntity]) -> str:
    if not entities:
        return "程序未从用户原文解析出有效实体；缺少必要实体时应向用户索取。"
    values = "；".join(
        f"{entity.field_name}={entity.value!r}（原文：{entity.source_text}）"
        for entity in entities
    )
    return (
        "以下实体由程序从用户原文确定性解析。调用工具时只能使用这些值，"
        f"不得自行改写或猜测：{values}"
    )


def create_order_agent():
    model = _create_model()
    understander = create_understander(model)
    agents: dict[tuple[str, ...], object] = {}

    def understand_node(state: AgentState) -> dict[str, object]:
        user_text = _latest_user_text(state["messages"])
        understanding, parsed_entities = understand_text(
            understander,
            user_text,
        )
        return {
            "understanding": understanding,
            "parsed_entities": parsed_entities,
        }

    def route_after_understand(state: AgentState) -> str:
        needs = state["understanding"].needs
        if InformationNeed.OUT_OF_SCOPE in needs:
            return "handoff"
        return "agent"

    def handoff_node(state: AgentState) -> dict[str, list[AIMessage]]:
        return {
            "messages": [
                AIMessage(
                    content="该请求超出当前只读拼团诊断能力范围，请联系人工客服处理。"
                )
            ]
        }

    def agent_node(
        state: AgentState,
        runtime: Runtime[AgentContext],
    ) -> dict[str, list[AnyMessage]]:
        needs = state["understanding"].needs
        tools = _select_tools(needs)
        cache_key = tuple(tool.name for tool in tools)
        inner_agent = agents.get(cache_key)
        if inner_agent is None:
            inner_agent = create_agent(
                model=model,
                tools=tools,
                context_schema=AgentContext,
                system_prompt=SYSTEM_PROMPT,
            )
            agents[cache_key] = inner_agent

        entity_message = SystemMessage(
            content=_entity_context(state.get("parsed_entities", []))
        )
        inner_messages = [entity_message, *state["messages"]]
        result = inner_agent.invoke(
            {"messages": inner_messages},
            context=runtime.context,
        )
        return {"messages": result["messages"][len(inner_messages):]}

    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("understand", understand_node)
    graph.add_node("handoff", handoff_node)
    graph.add_node("agent", agent_node)
    graph.add_edge(START, "understand")
    graph.add_conditional_edges(
        "understand",
        route_after_understand,
        {"handoff": "handoff", "agent": "agent"},
    )
    graph.add_edge("handoff", END)
    graph.add_edge("agent", END)
    return graph.compile()
