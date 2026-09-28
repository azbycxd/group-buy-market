from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES_PATH = PROJECT_ROOT / "docs" / "rules" / "group_buy_rules.md"
RULE_HEADING = re.compile(r"^##\s+([A-Z]+-\d{3})\s*$")
FIELD = re.compile(r"^-\s+(rule_id|section|title|content):\s*(.*)\s*$")


@dataclass(frozen=True, slots=True)
class RuleChunk:
    rule_id: str
    section: str
    title: str
    content: str

    @property
    def text(self) -> str:
        return f"{self.section} {self.title} {self.content}"


def _plain(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("`") and stripped.endswith("`"):
        return stripped[1:-1]
    return stripped


def load_rule_chunks(path: str | Path = DEFAULT_RULES_PATH) -> list[RuleChunk]:
    rule_path = Path(path)
    chunks: list[RuleChunk] = []
    current_heading: str | None = None
    current: dict[str, str] = {}

    def flush() -> None:
        nonlocal current_heading, current
        if current_heading is None:
            return
        missing = {"rule_id", "section", "title", "content"}.difference(
            current
        )
        if missing:
            raise ValueError(
                f"规则 {current_heading} 缺少字段: {sorted(missing)}"
            )
        if current["rule_id"] != current_heading:
            raise ValueError(
                f"规则标题 {current_heading} 与 rule_id {current['rule_id']} 不一致"
            )
        chunks.append(RuleChunk(**current))
        current_heading = None
        current = {}

    for line in rule_path.read_text(encoding="utf-8").splitlines():
        heading = RULE_HEADING.match(line)
        if heading:
            flush()
            current_heading = heading.group(1)
            continue
        if current_heading is None:
            continue
        field = FIELD.match(line)
        if field:
            current[field.group(1)] = _plain(field.group(2))
    flush()

    if not chunks:
        raise ValueError(f"规则文档没有可解析的规则: {rule_path}")
    rule_ids = [chunk.rule_id for chunk in chunks]
    if len(rule_ids) != len(set(rule_ids)):
        raise ValueError("规则文档包含重复 rule_id")
    return chunks
