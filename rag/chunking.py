from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES_PATH = PROJECT_ROOT / "docs" / "rules" / "group_buy_rules.md"
RULE_HEADING = re.compile(r"^##\s+([A-Z]+-\d{3})\s*$")
FIELD = re.compile(
    r"^-\s+(rule_id|section|title|content|visibility):\s*(.*)\s*$"
)


@dataclass(frozen=True, slots=True)
class RuleChunk:
    rule_id: str
    section: str
    title: str
    content: str
    source: tuple[str, ...] = ()
    visibility: str = "public"

    @property
    def text(self) -> str:
        return f"{self.title} {self.content}"


def _plain(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("`") and stripped.endswith("`"):
        return stripped[1:-1]
    return stripped


def load_rule_chunks(
    path: str | Path = DEFAULT_RULES_PATH,
    *,
    include_internal: bool = False,
) -> list[RuleChunk]:
    rule_path = Path(path)
    chunks: list[RuleChunk] = []
    seen_rule_ids: set[str] = set()
    current_heading: str | None = None
    current: dict[str, str] = {}
    current_source: list[str] = []
    in_source = False

    def flush() -> None:
        nonlocal current_heading, current, current_source, in_source
        if current_heading is None:
            return
        missing = {
            "rule_id",
            "section",
            "title",
            "content",
            "visibility",
        }.difference(current)
        if missing:
            raise ValueError(
                f"规则 {current_heading} 缺少字段: {sorted(missing)}"
            )
        if current["rule_id"] != current_heading:
            raise ValueError(
                f"规则标题 {current_heading} 与 rule_id {current['rule_id']} 不一致"
            )
        if current["rule_id"] in seen_rule_ids:
            raise ValueError(f"规则文档包含重复 rule_id: {current['rule_id']}")
        seen_rule_ids.add(current["rule_id"])
        if current["visibility"] not in {"public", "internal"}:
            raise ValueError(
                f"规则 {current_heading} visibility 非法: "
                f"{current['visibility']}"
            )
        if not current_source:
            raise ValueError(f"规则 {current_heading} 缺少 source")
        if current["visibility"] == "public" or include_internal:
            chunks.append(
                RuleChunk(
                    rule_id=current["rule_id"],
                    section=current["section"],
                    title=current["title"],
                    content=current["content"],
                    source=tuple(current_source),
                    visibility=current["visibility"],
                )
            )
        current_heading = None
        current = {}
        current_source = []
        in_source = False

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
            in_source = False
            continue
        if line == "- source:":
            in_source = True
            continue
        if in_source and line.startswith("  - "):
            current_source.append(line[4:].strip())
    flush()

    if not chunks:
        raise ValueError(f"规则文档没有可解析的规则: {rule_path}")
    return chunks
