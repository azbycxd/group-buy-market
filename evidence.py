from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StrictStr


class EvidenceType(StrEnum):
    FACT = "FACT"
    RULE = "RULE"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: StrictStr
    type: EvidenceType
    value: Any


def merge_evidence(
    current: list[Evidence],
    incoming: list[Evidence],
) -> list[Evidence]:
    merged: list[Evidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in [*current, *incoming]:
        key = (
            item.path,
            item.type.value,
            json.dumps(item.value, ensure_ascii=False, sort_keys=True),
        )
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


EvidenceList = Annotated[list[Evidence], merge_evidence]
