from dataclasses import dataclass
from typing import Any


@dataclass
class AgentContext:
    user_id: str
    parsed_entities: tuple[Any, ...] = ()
    user_text: str = ""
