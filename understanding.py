from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from tools.arguments import ActivityIdArguments, OrderFactsArguments


class InformationNeed(StrEnum):
    ORDER_STATUS = "ORDER_STATUS"
    ACTIVITY_VALIDITY = "ACTIVITY_VALIDITY"
    USER_ELIGIBILITY = "USER_ELIGIBILITY"
    JOINABLE_TEAMS = "JOINABLE_TEAMS"
    RULE_EXPLANATION = "RULE_EXPLANATION"
    REFUND_ARRIVAL = "REFUND_ARRIVAL"
    REFUND_REQUEST = "REFUND_REQUEST"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class EntityType(StrEnum):
    ORDER = "ORDER"
    ACTIVITY = "ACTIVITY"


class EntityMention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: EntityType
    source_text: str = Field(
        description="用户原文中包含实体值的原样片段，不能改写或补全"
    )


class Understanding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs: list[InformationNeed] = Field(min_length=1)
    entities: list[EntityMention] = Field(default_factory=list)


@dataclass(frozen=True)
class ParsedEntity:
    entity_type: EntityType
    source_text: str
    field_name: str
    value: str | int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "entity_type": self.entity_type.value,
            "source_text": self.source_text,
            self.field_name: self.value,
        }


UNDERSTANDING_PROMPT = """你是拼团客服的需求理解节点。只做分类和原文实体摘录，不回答问题。

needs 只能从以下类型选择：
- ORDER_STATUS：查询已有订单的状态或订单能否继续参团。
- ACTIVITY_VALIDITY：查询活动状态、是否有效、是否过期。
- USER_ELIGIBILITY：查询当前用户资格、次数，或分析为什么不能参加。
- JOINABLE_TEAMS：查询是否存在可加入团队，或分析为什么不能参加。
- RULE_EXPLANATION：解释拼团规则。
- REFUND_ARRIVAL：查询退款是否到账、何时到账。
- REFUND_REQUEST：询问如何申请退款或退款条件。
- OUT_OF_SCOPE：要求修改、创建、取消业务数据，或不属于上述只读拼团咨询。

分类规则：
1. “为什么/为啥不能参加某活动”是综合诊断，同时选择 ACTIVITY_VALIDITY、USER_ELIGIBILITY、JOINABLE_TEAMS。
2. 明确要求修改订单状态等写操作时，只返回 OUT_OF_SCOPE，不要同时返回 ORDER_STATUS。
3. 查询退款到账选择 REFUND_ARRIVAL；咨询退款申请方法或条件选择 REFUND_REQUEST。
4. 一个问题确实包含多个独立需求时可返回多个 needs。

entities 只能包含 entity_type 和 source_text：
- 订单号用 ORDER，活动编号用 ACTIVITY。
- source_text 必须逐字摘自用户原文并包含实体值。
- 不要返回解析后的 activityId、outTradeNo 或任何额外字段。
- 原文没有明确实体值时不要猜测，entities 返回空列表。
"""


_ORDER_NUMBER = re.compile(
    r"(?<![A-Za-z0-9._-])(ORD[A-Za-z0-9._-]*)(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)
_NUMERIC_ORDER_NUMBER = re.compile(
    r"(?<![A-Za-z0-9._-])([0-9]{6,128})(?![A-Za-z0-9._-])"
)
_LABELED_NUMERIC_ORDER = re.compile(
    r"(?:订单(?:号)?|outTradeNo)\s*(?:是|为|=|:|：)?\s*"
    r"([0-9]{6,128})(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)
_BARE_PENDING_NUMERIC_ORDER = re.compile(
    r"\s*([0-9]{6,128})\s*[。.!！?？]?\s*"
)
_ACTIVITY_ID = re.compile(
    r"(?<![A-Za-z0-9._-])(?:活动\s*)?([0-9]+)(?![A-Za-z0-9._-])"
)


def _resolve_order_match(
    source_text: str,
    user_text: str,
    *,
    pending: bool = False,
) -> re.Match[str] | None:
    prefixed = _ORDER_NUMBER.search(source_text)
    if prefixed is not None:
        return prefixed

    numeric = _NUMERIC_ORDER_NUMBER.search(source_text)
    if numeric is None:
        return None
    if pending and _BARE_PENDING_NUMERIC_ORDER.fullmatch(user_text):
        return numeric
    return next(
        (
            match
            for match in _LABELED_NUMERIC_ORDER.finditer(user_text)
            if match.group(1) == numeric.group(1)
        ),
        None,
    )


def resolve_entities(
    user_text: str,
    mentions: list[EntityMention],
) -> list[ParsedEntity]:
    """Resolve model-copied text with deterministic parsers; discard invalid mentions."""
    parsed: list[ParsedEntity] = []
    seen: set[tuple[EntityType, str | int]] = set()

    for mention in mentions:
        source_text = mention.source_text.strip()
        if not source_text or source_text not in user_text:
            continue

        try:
            if mention.entity_type is EntityType.ORDER:
                match = _resolve_order_match(source_text, user_text)
                if match is None:
                    continue
                value = OrderFactsArguments.model_validate(
                    {"outTradeNo": match.group(1)}
                ).outTradeNo
                field_name = "outTradeNo"
            else:
                match = _ACTIVITY_ID.search(source_text)
                if match is None:
                    continue
                value = ActivityIdArguments.model_validate(
                    {"activityId": int(match.group(1))}
                ).activityId
                field_name = "activityId"
        except (TypeError, ValueError):
            continue

        key = (mention.entity_type, value)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(
            ParsedEntity(
                entity_type=mention.entity_type,
                source_text=source_text,
                field_name=field_name,
                value=value,
            )
        )

    return parsed


def resolve_pending_entities(
    user_text: str,
    missing_entities: list[EntityType],
) -> list[ParsedEntity]:
    """Deterministically parse values requested by a pending requirement."""
    parsed: list[ParsedEntity] = []

    for entity_type in dict.fromkeys(missing_entities):
        try:
            if entity_type is EntityType.ORDER:
                match = _resolve_order_match(
                    user_text,
                    user_text,
                    pending=True,
                )
                if match is None:
                    continue
                value = OrderFactsArguments.model_validate(
                    {"outTradeNo": match.group(1)}
                ).outTradeNo
                field_name = "outTradeNo"
            else:
                match = _ACTIVITY_ID.search(user_text)
                if match is None:
                    continue
                value = ActivityIdArguments.model_validate(
                    {"activityId": int(match.group(1))}
                ).activityId
                field_name = "activityId"
        except (TypeError, ValueError):
            continue

        parsed.append(
            ParsedEntity(
                entity_type=entity_type,
                source_text=match.group(0).strip(),
                field_name=field_name,
                value=value,
            )
        )

    return parsed


def create_understander(model: Any) -> Any:
    return model.with_structured_output(
        Understanding,
        method="function_calling",
        strict=True,
    )


def understand_text(
    understander: Any,
    user_text: str,
) -> tuple[Understanding, list[ParsedEntity]]:
    understanding = understander.invoke(
        [
            SystemMessage(content=UNDERSTANDING_PROMPT),
            HumanMessage(content=user_text),
        ]
    )
    parsed_entities = resolve_entities(user_text, understanding.entities)
    return understanding, parsed_entities
