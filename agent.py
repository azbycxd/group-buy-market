import json
import os
import sqlite3
from pathlib import Path
from typing import Annotated, TypedDict

import aiosqlite
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import (
    AnyMessage,
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.runtime import Runtime

from agent_middleware import create_agent_middleware
from evidence import (
    CLEAR_EVIDENCE,
    Evidence,
    EvidenceSignal,
    EvidenceType,
    merge_evidence,
)
from outcome import AgentOutcome, Claim, ClaimType, OutcomeKind
from requirement_tracker import (
    CAPABILITY_TABLE,
    RequirementDecision,
    RequirementStatus,
    RequirementTracker,
)
from tools.activity_facts import get_activity_facts
from tools.context import AgentContext
from tools.eligibility_facts import get_user_eligibility_facts
from tools.joinable_team_facts import get_joinable_team_facts
from tools.order_facts import get_order_facts
from tools.rule_search import search_group_buy_rules
from understanding import (
    EntityType,
    InformationNeed,
    ParsedEntity,
    Understanding,
    create_understander,
    resolve_pending_entities,
    understand_text,
)


load_dotenv(override=True)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints.sqlite"


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

OUTCOME_PROMPT = """你是最终回答结构化节点。根据草稿回答和 Evidence 清单生成 AgentOutcome。
- kind 必须是 ANSWER。
- 将草稿中的业务事实或规则结论写入 claims。
- FACT Claim 只能逐字引用清单中 type=FACT 的 path。
- RULE Claim 只能逐字引用清单中 type=RULE 的 path。
- 没有对应类型 Evidence 时，不得生成该类型 Claim，必须从最终回答删除该结论。
- 状态类 FACT 只能陈述查询到的状态；没有 RULE Evidence 时，不得推导该状态对应的业务后果。
- CAPABILITY Claim 用于能力边界或服务可用性，可以不引用 Evidence。
- 不得创造、改写或猜测 Evidence path。
- final_answer 使用中文简洁回答用户问题。
"""


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
    requirement_status: RequirementStatus | None
    pending_needs: list[InformationNeed]
    pending_missing_entities: list[EntityType]
    missing_evidence: list[str]
    agent_rounds: int
    evidence: Annotated[list[Evidence], merge_evidence]
    outcome: AgentOutcome | None


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
        evidence_name.split(".", 1)[0]
        for need in needs
        for evidence_name in CAPABILITY_TABLE[need].required_evidence
    }
    return [tool for tool in TOOL_ORDER if tool.name in selected_names]


def _latest_draft_answer(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls and message.content:
            if isinstance(message.content, str):
                return message.content
            return json.dumps(message.content, ensure_ascii=False)
    return ""


def _has_tool_code(messages: list[AnyMessage], code: str) -> bool:
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("code") == code:
            return True
    return False


def _has_empty_rule_search(messages: list[AnyMessage]) -> bool:
    for message in messages:
        if (
            not isinstance(message, ToolMessage)
            or message.name != "search_group_buy_rules"
            or not isinstance(message.content, str)
        ):
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("success") is True
            and isinstance(payload.get("data"), dict)
            and payload["data"].get("matches") == []
        ):
            return True
    return False


def _capability_outcome(kind: OutcomeKind, text: str) -> AgentOutcome:
    return AgentOutcome(
        kind=kind,
        claims=[Claim(text=text, type=ClaimType.CAPABILITY, evidence=[])],
        final_answer=text,
    )


def _claim_errors(outcome: AgentOutcome, evidence: list[Evidence]) -> list[str]:
    catalog = {item.path: item.type for item in evidence}
    errors: list[str] = []
    for claim in outcome.claims:
        missing_paths = [path for path in claim.evidence if path not in catalog]
        if missing_paths:
            errors.append(f"不存在的 Evidence: {', '.join(missing_paths)}")

        if claim.type in {ClaimType.FACT, ClaimType.RULE}:
            if not claim.evidence:
                errors.append(f"{claim.type.value} Claim 缺少 Evidence")
                continue
            expected_type = (
                EvidenceType.FACT
                if claim.type is ClaimType.FACT
                else EvidenceType.RULE
            )
            wrong_type = [
                path
                for path in claim.evidence
                if path in catalog and catalog[path] is not expected_type
            ]
            if wrong_type:
                errors.append(
                    f"{claim.type.value} Claim 引用了错误类型 Evidence: "
                    f"{', '.join(wrong_type)}"
                )
    return errors


def _validated_answer_outcome(
    generated: AgentOutcome,
    evidence: list[Evidence],
) -> AgentOutcome:
    if generated.kind is not OutcomeKind.ANSWER or _claim_errors(
        generated,
        evidence,
    ):
        return _capability_outcome(
            OutcomeKind.HANDOFF,
            "回答中的结论无法与查询到的事实对应，已转人工客服核实。",
        )
    return generated.model_copy(
        update={
            "final_answer": "\n".join(claim.text for claim in generated.claims)
        }
    )


def _request_input(decision: RequirementDecision) -> str:
    requested: list[str] = []
    if EntityType.ORDER in decision.missing_entities:
        requested.append("订单号")
    if EntityType.ACTIVITY in decision.missing_entities:
        requested.append("活动 ID")
    return f"请提供{'和'.join(requested)}，以便继续查询。"


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


def _merge_entities(
    existing: list[ParsedEntity],
    recovered: list[ParsedEntity],
) -> list[ParsedEntity]:
    merged: dict[tuple[EntityType, str | int], ParsedEntity] = {
        (entity.entity_type, entity.value): entity for entity in existing
    }
    for entity in recovered:
        merged[(entity.entity_type, entity.value)] = entity
    return list(merged.values())


def _checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            InformationNeed,
            EntityType,
            ParsedEntity,
            Understanding,
            RequirementStatus,
            Evidence,
            EvidenceSignal,
            EvidenceType,
            AgentOutcome,
            OutcomeKind,
            Claim,
            ClaimType,
        ]
    )


def _compile_order_agent(checkpointer: object):
    model = _create_model()
    understander = create_understander(model)
    outcome_generator = model.with_structured_output(
        AgentOutcome,
        method="function_calling",
        strict=True,
    )
    tracker = RequirementTracker()
    agents: dict[tuple[str, ...], object] = {}

    def understand_node(state: AgentState) -> dict[str, object]:
        user_text = _latest_user_text(state["messages"])
        pending_needs = state.get("pending_needs", [])
        pending_missing = state.get("pending_missing_entities", [])
        if (
            state.get("requirement_status") is RequirementStatus.NEED_USER_INPUT
            and pending_needs
            and pending_missing
        ):
            recovered = resolve_pending_entities(user_text, pending_missing)
            if recovered:
                parsed_entities = _merge_entities(
                    state.get("parsed_entities", []),
                    recovered,
                )
                available_types = {
                    entity.entity_type for entity in parsed_entities
                }
                remaining = [
                    entity_type
                    for entity_type in pending_missing
                    if entity_type not in available_types
                ]
                return {
                    "understanding": Understanding(
                        needs=pending_needs,
                        entities=[],
                    ),
                    "parsed_entities": parsed_entities,
                    "pending_missing_entities": remaining,
                }

        understanding, parsed_entities = understand_text(
            understander,
            user_text,
        )
        return {
            "understanding": understanding,
            "parsed_entities": parsed_entities,
            "requirement_status": None,
            "pending_needs": [],
            "pending_missing_entities": [],
            "missing_evidence": [],
            "agent_rounds": 0,
            "evidence": CLEAR_EVIDENCE,
            "outcome": None,
        }

    def agent_node(
        state: AgentState,
        runtime: Runtime[AgentContext],
    ) -> dict[str, object]:
        needs = state["understanding"].needs
        decision = tracker.evaluate(
            needs=needs,
            entities=state.get("parsed_entities", []),
            evidence=state.get("evidence", []),
        )
        if decision.status in {
            RequirementStatus.UNSUPPORTED,
            RequirementStatus.NEED_USER_INPUT,
        }:
            return {"requirement_status": decision.status}

        tools = _select_tools(needs)
        cache_key = tuple(tool.name for tool in tools)
        inner_agent = agents.get(cache_key)
        if inner_agent is None:
            inner_agent = create_agent(
                model=model,
                tools=tools,
                middleware=create_agent_middleware(),
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
            context=AgentContext(
                user_id=runtime.context.user_id,
                parsed_entities=tuple(state.get("parsed_entities", [])),
                user_text=_latest_user_text(state["messages"]),
                request_id=runtime.context.request_id,
            ),
        )
        return {
            "messages": result["messages"][len(inner_messages):],
            "agent_rounds": state.get("agent_rounds", 0) + 1,
            "evidence": result.get("evidence", []),
        }

    def answer_outcome(state: AgentState) -> AgentOutcome:
        evidence = state.get("evidence", [])
        draft_answer = _latest_draft_answer(state["messages"])
        generated = outcome_generator.invoke(
            [
                SystemMessage(content=OUTCOME_PROMPT),
                HumanMessage(
                    content=json.dumps(
                        {
                            "draft_answer": draft_answer,
                            "evidence": [
                                item.model_dump(mode="json") for item in evidence
                            ],
                        },
                        ensure_ascii=False,
                    )
                ),
            ],
            config={"run_name": "outcome"},
        )
        return _validated_answer_outcome(generated, evidence)

    def finalize_node(state: AgentState) -> dict[str, object]:
        decision = tracker.evaluate(
            needs=state["understanding"].needs,
            entities=state.get("parsed_entities", []),
            evidence=state.get("evidence", []),
        )
        update: dict[str, object] = {
            "requirement_status": decision.status,
            "pending_missing_entities": list(decision.missing_entities),
            "missing_evidence": list(decision.missing_evidence),
        }

        outcome: AgentOutcome | None = None
        if decision.status is RequirementStatus.UNSUPPORTED:
            reasons = [
                CAPABILITY_TABLE[need].unsupported_reason
                for need in decision.unsupported_needs
                if CAPABILITY_TABLE[need].unsupported_reason
            ]
            reason = "".join(reasons) or "该请求超出当前拼团诊断能力范围。"
            outcome = _capability_outcome(
                OutcomeKind.HANDOFF,
                f"{reason}请联系人工客服处理。",
            )
        elif decision.status is RequirementStatus.NEED_USER_INPUT:
            update["pending_needs"] = list(state["understanding"].needs)
            outcome = _capability_outcome(
                OutcomeKind.REQUEST_INPUT,
                _request_input(decision),
            )
        elif decision.status is RequirementStatus.NEED_MORE_EVIDENCE:
            update["pending_needs"] = []
            if (
                any(
                    path.startswith("search_group_buy_rules.matches.")
                    for path in decision.missing_evidence
                )
                and _has_empty_rule_search(state["messages"])
            ):
                outcome = _capability_outcome(
                    OutcomeKind.HANDOFF,
                    "未检索到支持该问题的拼团规则，请联系人工客服处理。",
                )
            elif state.get("agent_rounds", 0) >= 2:
                if _has_tool_code(state["messages"], "SERVICE_UNAVAILABLE"):
                    outcome = _capability_outcome(
                        OutcomeKind.ANSWER,
                        "业务服务暂时不可用，暂时无法查询。",
                    )
                else:
                    outcome = _capability_outcome(
                        OutcomeKind.HANDOFF,
                        "缺少完成查询所需的业务证据，请联系人工客服处理。",
                    )
            else:
                missing = "、".join(decision.missing_evidence)
                update["messages"] = [
                    SystemMessage(
                        content=(
                            "RequirementTracker 判定仍缺少以下 Evidence path："
                            f"{missing}。请调用对应 Tool 后再回答。"
                        )
                    )
                ]
        elif decision.status is RequirementStatus.ANSWERABLE:
            update["pending_needs"] = []
            outcome = answer_outcome(state)

        if outcome is not None:
            update["outcome"] = outcome
            update["messages"] = [AIMessage(content=outcome.final_answer)]
        return update

    def route_after_finalize(state: AgentState) -> str:
        if (
            state["requirement_status"]
            is RequirementStatus.NEED_MORE_EVIDENCE
            and state.get("agent_rounds", 0) < 2
        ):
            return "agent"
        return "end"

    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("understand", understand_node)
    graph.add_node("agent", agent_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "agent")
    graph.add_edge("agent", "finalize")
    graph.add_conditional_edges(
        "finalize",
        route_after_finalize,
        {"agent": "agent", "end": END},
    )
    return graph.compile(checkpointer=checkpointer)


def create_order_agent(
    checkpoint_path: str | Path = DEFAULT_CHECKPOINT_PATH,
):
    checkpoint_file = Path(checkpoint_path).resolve()
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_connection = sqlite3.connect(
        checkpoint_file,
        check_same_thread=False,
    )
    checkpointer = SqliteSaver(
        checkpoint_connection,
        serde=_checkpoint_serde(),
    )
    try:
        return _compile_order_agent(checkpointer)
    except Exception:
        checkpoint_connection.close()
        raise


async def create_async_order_agent(
    checkpoint_path: str | Path = DEFAULT_CHECKPOINT_PATH,
):
    checkpoint_file = Path(checkpoint_path).resolve()
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_connection = await aiosqlite.connect(checkpoint_file)
    checkpointer = AsyncSqliteSaver(
        checkpoint_connection,
        serde=_checkpoint_serde(),
    )
    try:
        return _compile_order_agent(checkpointer)
    except Exception:
        await checkpoint_connection.close()
        raise


def close_order_agent(agent: object) -> None:
    checkpointer = getattr(agent, "checkpointer", None)
    connection = getattr(checkpointer, "conn", None)
    if connection is not None:
        connection.close()


async def close_async_order_agent(agent: object) -> None:
    checkpointer = getattr(agent, "checkpointer", None)
    connection = getattr(checkpointer, "conn", None)
    if connection is not None:
        await connection.close()
