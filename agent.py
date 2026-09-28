import json
import os
import re
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
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.runtime import Runtime

from action_ledger import AgentActionStore
from agent_middleware import create_agent_middleware
from confirmation_workflow import start_confirmation_workflow
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
from tools.facts import RefundPreviewFacts
from tools.refund_preview import get_refund_preview
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
- RULE Claim 必须填写 rule_id，并且只能使用同一条当前 type=RULE Evidence 中真实存在的 rule_id 和 path。
- 每条 RULE Claim 只对应一个 rule_id；text 只写规则结论正文，不要自行生成“依据《标题》”引用，标题由程序从 Evidence 重建。
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
    proposed_action_id: str | None


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
    rule_catalog = {
        item.path: item.value
        for item in evidence
        if item.type is EvidenceType.RULE
        and isinstance(item.value, dict)
        and isinstance(item.value.get("rule_id"), str)
        and isinstance(item.value.get("title"), str)
    }
    errors: list[str] = []
    for claim in outcome.claims:
        missing_paths = [path for path in claim.evidence if path not in catalog]
        if missing_paths:
            errors.append(f"不存在的 Evidence: {', '.join(missing_paths)}")

        if claim.type in {ClaimType.FACT, ClaimType.RULE}:
            if not claim.evidence:
                errors.append(f"{claim.type.value} Claim 缺少 Evidence")
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
        if claim.type is ClaimType.RULE:
            if not claim.rule_id:
                errors.append("RULE Claim 缺少 rule_id")
                continue
            matching_paths = {
                path
                for path, match in rule_catalog.items()
                if match["rule_id"] == claim.rule_id
            }
            if not matching_paths:
                errors.append(
                    f"RULE Claim 的 rule_id 不在本轮检索结果中: {claim.rule_id}"
                )
                continue
            referenced_rule_paths = {
                path for path in claim.evidence if path in rule_catalog
            }
            if not referenced_rule_paths.intersection(matching_paths):
                errors.append(
                    "RULE Claim 未引用其 rule_id 对应的 Evidence: "
                    f"{claim.rule_id}"
                )
            foreign_paths = referenced_rule_paths.difference(matching_paths)
            if foreign_paths:
                errors.append(
                    "RULE Claim 引用了其他 rule_id 的 Evidence: "
                    f"{', '.join(sorted(foreign_paths))}"
                )
    return errors


_MODEL_RULE_CITATION = re.compile(r"^\s*依据《[^》]*》[\s，,：:。]*")


def _validated_claims(
    generated: AgentOutcome,
    evidence: list[Evidence],
) -> list[Claim]:
    rule_matches = {
        str(item.value["rule_id"]): item.value
        for item in evidence
        if item.type is EvidenceType.RULE
        and isinstance(item.value, dict)
        and isinstance(item.value.get("rule_id"), str)
        and isinstance(item.value.get("title"), str)
    }
    validated: list[Claim] = []
    for claim in generated.claims:
        if claim.type is not ClaimType.RULE:
            validated.append(claim)
            continue
        match = rule_matches[claim.rule_id or ""]
        body = _MODEL_RULE_CITATION.sub("", claim.text).strip()
        validated.append(
            claim.model_copy(
                update={"text": f"依据《{match['title']}》：{body}"}
            )
        )
    return validated


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
    claims = _validated_claims(generated, evidence)
    return generated.model_copy(
        update={
            "claims": claims,
            "final_answer": "\n".join(claim.text for claim in claims),
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


def _refund_evidence(
    order_result: dict[str, object],
    preview: RefundPreviewFacts,
) -> list[Evidence]:
    order_data = order_result.get("data")
    order_status = None
    if isinstance(order_data, dict) and isinstance(order_data.get("order"), dict):
        order_status = order_data["order"].get("status")
    return [
        Evidence(
            path="get_order_facts.order.status",
            type=EvidenceType.FACT,
            value=order_status,
        ),
        Evidence(
            path="refund_preview.orderStatus",
            type=EvidenceType.FACT,
            value=preview.order_status,
        ),
        Evidence(
            path="refund_preview.teamStatus",
            type=EvidenceType.FACT,
            value=preview.team_status,
        ),
        Evidence(
            path="refund_preview.refundType",
            type=EvidenceType.FACT,
            value=preview.refund_type,
        ),
        Evidence(
            path="refund_preview.refundProposalAllowed",
            type=EvidenceType.FACT,
            value=preview.refund_proposal_allowed,
        ),
        Evidence(
            path="refund_preview.requiresManualReview",
            type=EvidenceType.FACT,
            value=preview.requires_manual_review,
        ),
        Evidence(
            path="refund_preview.orderUpdateTime",
            type=EvidenceType.FACT,
            value=preview.order_update_time,
        ),
        Evidence(
            path="refund_preview.teamUpdateTime",
            type=EvidenceType.FACT,
            value=preview.team_update_time,
        ),
    ]


def _refund_outcome(
    *,
    kind: OutcomeKind,
    preview: RefundPreviewFacts,
    capability_texts: list[str],
    evidence: list[Evidence],
) -> AgentOutcome:
    claims = [
        Claim(
            text=f"当前订单状态：{preview.order_status}。",
            type=ClaimType.FACT,
            evidence=["refund_preview.orderStatus"],
        ),
        Claim(
            text=f"退款类型：{preview.refund_type}。",
            type=ClaimType.FACT,
            evidence=["refund_preview.refundType"],
        ),
        *[
            Claim(text=text, type=ClaimType.CAPABILITY, evidence=[])
            for text in capability_texts
        ],
    ]
    outcome = AgentOutcome(
        kind=kind,
        claims=claims,
        final_answer="\n".join(claim.text for claim in claims),
    )
    if _claim_errors(outcome, evidence):
        return _capability_outcome(
            OutcomeKind.HANDOFF,
            "退款预览无法与查询事实对应，已转人工客服核实。",
        )
    return outcome


def _refund_failure_outcome(result: dict[str, object]) -> AgentOutcome:
    code = result.get("code")
    if code == "ORDER_NOT_FOUND_OR_NOT_AUTHORIZED":
        return _capability_outcome(
            OutcomeKind.HANDOFF,
            "订单不存在或无权限，无法生成退款提议，请联系人工客服核实。",
        )
    if code in {"SERVICE_UNAVAILABLE", "INVALID_TOOL_RESPONSE"}:
        return _capability_outcome(
            OutcomeKind.ANSWER,
            "业务服务暂时不可用，暂时无法生成退款提议。C1 未执行任何退款。",
        )
    return _capability_outcome(
        OutcomeKind.HANDOFF,
        "暂时无法核实退款预览，已转人工客服处理。C1 未执行任何退款。",
    )


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


def _compile_order_agent(
    checkpointer: object,
    checkpoint_path: Path,
):
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
            "proposed_action_id": None,
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

    def propose_refund_node(
        state: AgentState,
        runtime: Runtime[AgentContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        decision = tracker.evaluate(
            needs=state["understanding"].needs,
            entities=state.get("parsed_entities", []),
            evidence=state.get("evidence", []),
        )
        if decision.status is RequirementStatus.NEED_USER_INPUT:
            outcome = _capability_outcome(
                OutcomeKind.REQUEST_INPUT,
                _request_input(decision),
            )
            return {
                "requirement_status": decision.status,
                "pending_needs": list(state["understanding"].needs),
                "pending_missing_entities": list(decision.missing_entities),
                "missing_evidence": [],
                "outcome": outcome,
                "messages": [AIMessage(content=outcome.final_answer)],
            }

        order_entity = next(
            (
                entity
                for entity in state.get("parsed_entities", [])
                if entity.entity_type is EntityType.ORDER
            ),
            None,
        )
        if order_entity is None or not isinstance(order_entity.value, str):
            outcome = _capability_outcome(
                OutcomeKind.REQUEST_INPUT,
                "请提供订单号，以便生成退款预览。",
            )
            return {
                "requirement_status": RequirementStatus.NEED_USER_INPUT,
                "pending_needs": [InformationNeed.REFUND_REQUEST],
                "pending_missing_entities": [EntityType.ORDER],
                "outcome": outcome,
                "messages": [AIMessage(content=outcome.final_answer)],
            }

        order_result = get_order_facts.func(
            outTradeNo=order_entity.value,
            runtime=runtime,
        )
        if order_result.get("success") is not True:
            outcome = _refund_failure_outcome(order_result)
            return {
                "requirement_status": RequirementStatus.ANSWERABLE,
                "pending_needs": [],
                "pending_missing_entities": [],
                "outcome": outcome,
                "messages": [AIMessage(content=outcome.final_answer)],
            }

        preview_result = get_refund_preview(
            out_trade_no=order_entity.value,
            user_id=runtime.context.user_id,
            request_id=runtime.context.request_id,
        )
        if preview_result.get("success") is not True:
            outcome = _refund_failure_outcome(preview_result)
            return {
                "requirement_status": RequirementStatus.ANSWERABLE,
                "pending_needs": [],
                "pending_missing_entities": [],
                "outcome": outcome,
                "messages": [AIMessage(content=outcome.final_answer)],
            }

        preview = RefundPreviewFacts.model_validate(preview_result["data"])
        evidence = _refund_evidence(order_result, preview)
        proposed_action_id: str | None = None
        common_texts = [
            "当前仅生成只读退款预览，没有执行任何退款。",
            (
                "退款资金是否到账不在当前系统事实范围内，"
                "本系统不承诺退款到账。"
            ),
        ]

        if (
            preview.order_status == "CLOSE"
            or not preview.refund_proposal_allowed
        ):
            outcome = _refund_outcome(
                kind=OutcomeKind.HANDOFF,
                preview=preview,
                capability_texts=[
                    "该订单不能再次退款，未写入 PROPOSED 退款提议。",
                    *common_texts,
                ],
                evidence=evidence,
            )
        elif preview.requires_manual_review:
            outcome = _refund_outcome(
                kind=OutcomeKind.HANDOFF,
                preview=preview,
                capability_texts=[
                    (
                        "该退款需要人工审核；只有审核通过并在未来确认后，"
                        f"才会按 {preview.refund_type} 路径发起退款操作。"
                    ),
                    "当前未写入可确认的 PROPOSED 退款提议。",
                    *common_texts,
                ],
                evidence=evidence,
            )
        else:
            session_id = str(
                config.get("configurable", {}).get("thread_id", "")
            )
            if not session_id:
                outcome = _capability_outcome(
                    OutcomeKind.HANDOFF,
                    "缺少会话标识，无法记录退款提议。当前未执行任何退款。",
                )
            else:
                try:
                    action_store = AgentActionStore()
                    action, created = action_store.create_or_reuse_refund_proposal(
                        session_id=session_id,
                        user_id=runtime.context.user_id,
                        out_trade_no=order_entity.value,
                        preview=preview.model_dump(mode="json", by_alias=True),
                        expected_version=(
                            f"{preview.order_update_time}|"
                            f"{preview.team_update_time}"
                        ),
                    )
                    if created:
                        start_confirmation_workflow(
                            checkpoint_path,
                            action,
                        )
                except (sqlite3.Error, OSError, RuntimeError, ValueError):
                    outcome = _capability_outcome(
                        OutcomeKind.HANDOFF,
                        "退款提议或确认流程暂时不可用，未执行任何退款。",
                    )
                else:
                    proposed_action_id = str(action["action_id"])
                    outcome = _refund_outcome(
                        kind=OutcomeKind.ANSWER,
                        preview=preview,
                        capability_texts=[
                            (
                                "已生成退款提议，请在 5 分钟内通过确认操作"
                                "完成确认。在普通聊天中说“我确认”不会生效。"
                            ),
                            (
                                "如果未来由用户确认，将为订单 "
                                f"{order_entity.value} 按 {preview.refund_type} "
                                "路径发起退款操作。"
                            ),
                            *common_texts,
                        ],
                        evidence=evidence,
                    )

        return {
            "requirement_status": RequirementStatus.ANSWERABLE,
            "pending_needs": [],
            "pending_missing_entities": [],
            "missing_evidence": [],
            "evidence": evidence,
            "outcome": outcome,
            "proposed_action_id": proposed_action_id,
            "messages": [AIMessage(content=outcome.final_answer)],
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

    def route_after_understand(state: AgentState) -> str:
        needs = state["understanding"].needs
        if (
            InformationNeed.REFUND_REQUEST in needs
            and InformationNeed.OUT_OF_SCOPE not in needs
        ):
            return "propose_refund"
        return "agent"

    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("understand", understand_node)
    graph.add_node("agent", agent_node)
    graph.add_node("propose_refund", propose_refund_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "understand")
    graph.add_conditional_edges(
        "understand",
        route_after_understand,
        {"propose_refund": "propose_refund", "agent": "agent"},
    )
    graph.add_edge("propose_refund", END)
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
        return _compile_order_agent(checkpointer, checkpoint_file)
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
        return _compile_order_agent(checkpointer, checkpoint_file)
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
