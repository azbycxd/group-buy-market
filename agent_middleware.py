from __future__ import annotations

import json
import re
from typing import Any

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    wrap_tool_call,
)
from langchain_core.messages import AIMessage, ToolMessage

from understanding import EntityType


ENTITY_PARAMETERS: dict[str, dict[str, EntityType]] = {
    "get_order_facts": {"outTradeNo": EntityType.ORDER},
    "get_activity_facts": {"activityId": EntityType.ACTIVITY},
    "get_user_eligibility_facts": {"activityId": EntityType.ACTIVITY},
    "get_joinable_team_facts": {"activityId": EntityType.ACTIVITY},
}
FREE_TEXT_PARAMETERS: dict[str, tuple[str, ...]] = {
    "search_group_buy_rules": ("query",),
}


def _error_message(request: Any, code: str, message: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(
            {
                "success": False,
                "code": code,
                "message": message,
                "retryable": False,
                "data": None,
            },
            ensure_ascii=False,
        ),
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
        status="error",
    )


def _parse_tool_payload(message: ToolMessage) -> dict[str, Any] | None:
    if message.status == "error":
        return None
    if isinstance(message.artifact, dict):
        payload = message.artifact
    elif isinstance(message.content, str):
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            return None
    else:
        return None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    return payload


def _collect_observation_entities(value: Any) -> dict[EntityType, set[str | int]]:
    trusted: dict[EntityType, set[str | int]] = {
        EntityType.ORDER: set(),
        EntityType.ACTIVITY: set(),
    }

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    key in {"activityId", "activity_id"}
                    and isinstance(child, int)
                    and not isinstance(child, bool)
                    and child > 0
                ):
                    trusted[EntityType.ACTIVITY].add(child)
                elif (
                    key in {"outTradeNo", "out_trade_no"}
                    and isinstance(child, str)
                    and child
                ):
                    trusted[EntityType.ORDER].add(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return trusted


def _trusted_values(request: Any) -> dict[EntityType, set[str | int]]:
    trusted: dict[EntityType, set[str | int]] = {
        EntityType.ORDER: set(),
        EntityType.ACTIVITY: set(),
    }
    context = request.runtime.context
    for entity in getattr(context, "parsed_entities", ()):
        trusted[entity.entity_type].add(entity.value)

    for message in request.state.get("messages", []):
        if not isinstance(message, ToolMessage):
            continue
        payload = _parse_tool_payload(message)
        if payload is None:
            continue
        observed = _collect_observation_entities(payload.get("data"))
        for entity_type, values in observed.items():
            trusted[entity_type].update(values)
    return trusted


def _comes_from_other_semantic(
    value: Any,
    expected_type: EntityType,
    request: Any,
) -> bool:
    candidate = str(value)
    for entity in getattr(request.runtime.context, "parsed_entities", ()):
        if entity.entity_type is expected_type:
            continue
        if candidate == str(entity.value) or candidate == entity.source_text:
            return True
        if candidate and candidate in entity.source_text:
            return True
    return False


def _entity_error(
    parameter: str,
    value: Any,
    expected_type: EntityType,
    request: Any,
) -> str | None:
    if expected_type is EntityType.ACTIVITY:
        valid_type = isinstance(value, int) and not isinstance(value, bool) and value > 0
        other_label = "订单号"
    else:
        valid_type = isinstance(value, str) and bool(value.strip())
        other_label = "activityId"

    if _comes_from_other_semantic(value, expected_type, request):
        return f"参数 {parameter} 语义不匹配，不能使用{other_label}来源。"
    if not valid_type:
        return f"参数 {parameter} 类型不正确，且没有匹配的可信来源。"

    trusted = _trusted_values(request)
    if value not in trusted[expected_type]:
        return f"参数 {parameter}={value!r} 无可信来源，禁止执行 Tool。"
    return None


def _text_anchors(value: str) -> set[str]:
    lowered = value.lower()
    latin = {
        token
        for token in re.findall(r"[a-z0-9]+", lowered)
        if len(token) >= 2
    }
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    bigrams = {
        chinese[index : index + 2]
        for index in range(max(0, len(chinese) - 1))
    }
    return latin | bigrams


def _query_error(value: Any, request: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "参数 query 类型不正确，且没有匹配的可信来源。"
    user_text = getattr(request.runtime.context, "user_text", "").strip()
    normalized_query = re.sub(r"\s+", "", value).lower()
    normalized_user = re.sub(r"\s+", "", user_text).lower()
    if normalized_query in normalized_user or normalized_user in normalized_query:
        return None
    if _text_anchors(value) & _text_anchors(user_text):
        return None
    return "参数 query 无法追溯到用户原文，禁止执行 Tool。"


@wrap_tool_call
def GroundingGuard(request: Any, handler: Any) -> ToolMessage:
    """Block tool arguments that do not have a type-safe, semantic source."""
    tool_name = request.tool_call["name"]
    arguments = request.tool_call.get("args", {})

    if tool_name in ENTITY_PARAMETERS:
        policy = ENTITY_PARAMETERS[tool_name]
        unexpected = set(arguments).difference(policy)
        if unexpected:
            names = "、".join(sorted(unexpected))
            return _error_message(
                request,
                "GROUNDING_BLOCKED",
                f"Tool 参数 {names} 没有 Grounding 策略，禁止执行。",
            )
        for parameter, entity_type in policy.items():
            if parameter not in arguments:
                return _error_message(
                    request,
                    "GROUNDING_BLOCKED",
                    f"参数 {parameter} 缺失可信来源，禁止执行 Tool。",
                )
            if error := _entity_error(
                parameter,
                arguments[parameter],
                entity_type,
                request,
            ):
                return _error_message(request, "GROUNDING_BLOCKED", error)
    elif tool_name in FREE_TEXT_PARAMETERS:
        allowed = set(FREE_TEXT_PARAMETERS[tool_name])
        unexpected = set(arguments).difference(allowed)
        if unexpected or "query" not in arguments:
            return _error_message(
                request,
                "GROUNDING_BLOCKED",
                "规则搜索参数不符合 Grounding 策略，禁止执行 Tool。",
            )
        if error := _query_error(arguments["query"], request):
            return _error_message(request, "GROUNDING_BLOCKED", error)
    else:
        return _error_message(
            request,
            "GROUNDING_BLOCKED",
            f"Tool {tool_name} 没有 Grounding 策略，禁止执行。",
        )

    return handler(request)


def _call_signature(call: dict[str, Any]) -> tuple[str, str]:
    arguments = json.dumps(
        call.get("args", {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return call["name"], arguments


def _previous_identical_calls(request: Any) -> int:
    current_id = request.tool_call["id"]
    signature = _call_signature(request.tool_call)
    count = 0
    for message in request.state.get("messages", []):
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            if call["id"] == current_id:
                return count
            if _call_signature(call) == signature:
                count += 1
    return count


@wrap_tool_call
def RepeatGuard(request: Any, handler: Any) -> ToolMessage:
    """Allow one execution for each unique Tool name and argument combination."""
    if _previous_identical_calls(request) >= 1:
        return _error_message(
            request,
            "REPEAT_CALL_BLOCKED",
            "相同 Tool 和参数已经调用过，禁止重复执行。",
        )
    return handler(request)


def create_agent_middleware() -> list[Any]:
    return [
        RepeatGuard,
        GroundingGuard,
        ToolCallLimitMiddleware(run_limit=8, exit_behavior="continue"),
        ModelCallLimitMiddleware(run_limit=6, exit_behavior="end"),
    ]
