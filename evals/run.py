from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import LLMResult

from evidence import EvidenceType
from outcome import AgentOutcome, ClaimType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).with_name("cases.yaml")
VALID_ACTIONS = {"ANSWER", "REQUEST_INPUT", "HANDOFF"}
RUNS_PER_CASE = int(os.getenv("EVAL_RUNS_PER_CASE", "3"))
FINAL_RESULTS_PATH = PROJECT_ROOT / "evals" / "results" / "final_69x3.json"
FINAL_REPORT_PATH = PROJECT_ROOT / "docs" / "final_eval.md"
FAILURE_STAGES = {
    "UNDERSTANDING",
    "ENTITY_RESOLUTION",
    "REQUIREMENT_TRACKER",
    "TOOL_SELECTION",
    "TOOL_EXECUTION",
    "RAG_RETRIEVAL",
    "CLAIM_VALIDATION",
    "OUTCOME_COMPOSITION",
    "EVAL_ORACLE",
    "OTHER",
}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


@dataclass
class EvalRun:
    passed: bool
    actual_action: str
    tools: list[str]
    evidence_paths: list[str]
    understanding: list[str]
    rule_queries: list[str]
    rule_matches: list[dict[str, Any]]
    rule_claims: list[dict[str, Any]]
    final_answer: str
    input_tokens: int | None
    output_tokens: int | None
    token_count: int
    latency_seconds: float
    reasons: list[str]
    failure_stage: str | None
    parsed_entities: list[dict[str, Any]]
    requirement_status: str | None
    tool_results: list[dict[str, Any]]


@dataclass
class CaseRuns:
    case: dict[str, Any]
    runs: list[EvalRun]


class TokenCounter(BaseCallbackHandler):
    def __init__(self) -> None:
        self.total_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._saw_usage = False
        self._breakdown_complete = True
        self._lock = threading.Lock()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        handled = False
        for generation_list in response.generations:
            for generation in generation_list:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if isinstance(usage, dict):
                    handled = self._record_usage(usage) or handled

        if not handled and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage") or response.llm_output.get(
                "usage"
            )
            if isinstance(usage, dict):
                self._record_usage(usage)

    def _record_usage(self, usage: dict[str, Any]) -> bool:
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get(
            "output_tokens", usage.get("completion_tokens")
        )
        total_tokens = usage.get("total_tokens")
        if not isinstance(total_tokens, int) and isinstance(
            input_tokens, int
        ) and isinstance(output_tokens, int):
            total_tokens = input_tokens + output_tokens
        if not isinstance(total_tokens, int):
            return False

        with self._lock:
            self._saw_usage = True
            self.total_tokens += total_tokens
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                self.input_tokens += input_tokens
                self.output_tokens += output_tokens
            else:
                self._breakdown_complete = False
        return True

    def usage(self) -> TokenUsage:
        if not self._saw_usage:
            return TokenUsage(None, None, None)
        return TokenUsage(
            input_tokens=(
                self.input_tokens if self._breakdown_complete else None
            ),
            output_tokens=(
                self.output_tokens if self._breakdown_complete else None
            ),
            total_tokens=self.total_tokens,
        )


def load_cases() -> list[dict[str, Any]]:
    with CASES_PATH.open("r", encoding="utf-8") as file:
        cases = yaml.safe_load(file)
    if not isinstance(cases, list) or not 50 <= len(cases) <= 70:
        raise ValueError("evals/cases.yaml 必须包含约 60 条 Case")

    seen_ids: set[str] = set()
    for case in cases:
        required = {
            "id",
            "category",
            "expected_action",
            "forbidden_claim_terms",
        }
        missing = required.difference(case)
        if missing:
            raise ValueError(f"Case {case.get('id')} 缺少字段: {sorted(missing)}")
        if case["id"] in seen_ids:
            raise ValueError(f"Case id 重复: {case['id']}")
        seen_ids.add(case["id"])
        if case["expected_action"] not in VALID_ACTIONS:
            raise ValueError(f"Case {case['id']} expected_action 非法")
        allowed_actions = case.get("allowed_actions", [case["expected_action"]])
        if (
            not isinstance(allowed_actions, list)
            or not allowed_actions
            or any(action not in VALID_ACTIONS for action in allowed_actions)
        ):
            raise ValueError(f"Case {case['id']} allowed_actions 非法")
        if ("input" in case) == ("turns" in case):
            raise ValueError(f"Case {case['id']} 必须且只能包含 input 或 turns")
        if "turns" in case and (
            not isinstance(case["turns"], list) or len(case["turns"]) < 2
        ):
            raise ValueError(f"Case {case['id']} turns 至少包含两轮")
    return cases


def select_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configured = os.getenv("EVAL_CASE_IDS", "").strip()
    if not configured:
        return cases
    requested = [item.strip() for item in configured.split(",") if item.strip()]
    by_id = {str(case["id"]): case for case in cases}
    unknown = [case_id for case_id in requested if case_id not in by_id]
    if unknown:
        raise ValueError(f"EVAL_CASE_IDS 包含未知 Case: {', '.join(unknown)}")
    return [by_id[case_id] for case_id in requested]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("fake_java 启动失败")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("等待 fake_java 启动超时")


def start_fake_java(port: int) -> subprocess.Popen[bytes]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "uvicorn",
            "fake_java.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    wait_until_ready(process, port)
    return process


def path_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path == pattern


def has_path(pattern: str, paths: list[str]) -> bool:
    return any(path_matches(pattern, path) for path in paths)


def invoke_case(
    agent: Any,
    context_type: Any,
    case: dict[str, Any],
    session_id: str | None = None,
) -> tuple[dict[str, Any], TokenUsage, float]:
    counter = TokenCounter()
    context = context_type(user_id=case.get("user_id", "demo-user"))
    turns = case.get("turns") or [case["input"]]
    session_id = session_id or f"eval-{case['id']}-{uuid.uuid4().hex}"
    unavailable_turns = set(case.get("service_unavailable_turns", []))
    original_base_url = os.environ.get("FAKE_JAVA_BASE_URL")
    result: dict[str, Any] = {}
    started_at = time.perf_counter()

    try:
        for turn_number, turn in enumerate(turns, start=1):
            if turn_number in unavailable_turns:
                os.environ["FAKE_JAVA_BASE_URL"] = "http://127.0.0.1:1"
            elif original_base_url is not None:
                os.environ["FAKE_JAVA_BASE_URL"] = original_base_url
            result = agent.invoke(
                {"messages": [HumanMessage(content=turn)]},
                context=context,
                config={
                    "callbacks": [counter],
                    "configurable": {"thread_id": session_id},
                },
            )
    finally:
        if original_base_url is None:
            os.environ.pop("FAKE_JAVA_BASE_URL", None)
        else:
            os.environ["FAKE_JAVA_BASE_URL"] = original_base_url

    return result, counter.usage(), time.perf_counter() - started_at


_TOOL_EXPECTED_NEEDS: dict[str, set[str]] = {
    "get_order_facts": {"ORDER_STATUS"},
    "get_activity_facts": {"ACTIVITY_VALIDITY"},
    "get_user_eligibility_facts": {"USER_ELIGIBILITY"},
    "get_joinable_team_facts": {"JOINABLE_TEAMS"},
    "search_group_buy_rules": {"RULE_EXPLANATION", "REFUND_POLICY"},
}


def _failure_stage(
    *,
    case: dict[str, Any],
    reasons: list[str],
    actual_action: str,
    tools: list[str],
    tool_results: list[dict[str, Any]],
    evidence_paths: list[str],
    understanding: list[str],
    parsed_entities: list[dict[str, Any]],
    requirement_status: str | None,
    rule_matches: list[dict[str, Any]],
) -> str | None:
    """Attribute a failed run from structured state and tool records."""
    if not reasons:
        return None
    reason_text = "\n".join(reasons)
    if "Understanding 不符合允许路径" in reason_text:
        return "UNDERSTANDING"
    if "final_answer 与 validated claims 不一致" in reason_text:
        return "OUTCOME_COMPOSITION"
    if any(
        marker in reason_text
        for marker in (
            "Claim 引用不存在 Evidence",
            "Claim Evidence 类型错误",
            "Claim 缺少 Evidence",
            "RULE Claim",
            "最终回答缺少真实标题引用",
        )
    ):
        return "CLAIM_VALIDATION"
    if actual_action == "ERROR":
        return "OTHER"
    if any(item.get("success") is False for item in tool_results):
        return "TOOL_EXECUTION"

    required_tools = set(case.get("required_tools", []))
    missing_tools = required_tools.difference(tools)
    if missing_tools:
        if "search_group_buy_rules" in missing_tools and {
            "ORDER_STATUS",
            "ACTIVITY_VALIDITY",
            "USER_ELIGIBILITY",
            "JOINABLE_TEAMS",
            "REFUND_REQUEST",
        }.intersection(understanding):
            # A generic rule question acquired a concrete-fact/action need and
            # was consequently blocked on an entity the case never required.
            return "UNDERSTANDING"
        expected_needs = set().union(
            *(_TOOL_EXPECTED_NEEDS.get(tool, set()) for tool in missing_tools)
        )
        if expected_needs and not expected_needs.intersection(understanding):
            return "UNDERSTANDING"
        if actual_action == "REQUEST_INPUT":
            if requirement_status == "NEED_USER_INPUT" and not parsed_entities:
                return "ENTITY_RESOLUTION"
            return "REQUIREMENT_TRACKER"
        return "TOOL_SELECTION"

    rule_expected = (
        "search_group_buy_rules" in required_tools
        or bool(case.get("required_rule_ids"))
        or bool(case.get("required_rule_ids_any"))
    )
    if rule_expected and "search_group_buy_rules" in tools and not rule_matches:
        return "RAG_RETRIEVAL"
    if "缺少 Evidence" in reason_text:
        return "TOOL_EXECUTION" if tools else "TOOL_SELECTION"
    if "调用禁用 Tool" in reason_text or "缺少精确 Tool 调用" in reason_text:
        return "TOOL_SELECTION"
    if "Rule matches 不符合预期" in reason_text:
        return "EVAL_ORACLE"
    if actual_action != case["expected_action"]:
        if actual_action == "REQUEST_INPUT":
            return (
                "ENTITY_RESOLUTION"
                if requirement_status == "NEED_USER_INPUT"
                else "REQUIREMENT_TRACKER"
            )
        if actual_action == "HANDOFF" and evidence_paths:
            return "CLAIM_VALIDATION"
        if "OUT_OF_SCOPE" in understanding:
            return "UNDERSTANDING"
        return "REQUIREMENT_TRACKER"
    return "OTHER"


def evaluate_result(
    result: dict[str, Any],
    case: dict[str, Any],
    token_usage: TokenUsage,
    latency_seconds: float,
) -> EvalRun:
    tool_call_records = [
        call
        for message in result.get("messages", [])
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    tool_calls = [call["name"] for call in tool_call_records]
    rule_queries = [
        str(call.get("args", {}).get("query", ""))
        for call in tool_call_records
        if call["name"] == "search_group_buy_rules"
    ]
    evidence = result.get("evidence", [])
    evidence_paths = [item.path for item in evidence]
    evidence_types = {item.path: item.type for item in evidence}
    current_rule_matches = {
        str(item.value["rule_id"]): {
            "path": item.path,
            **item.value,
        }
        for item in evidence
        if item.type is EvidenceType.RULE
        and isinstance(item.value, dict)
        and isinstance(item.value.get("rule_id"), str)
        and isinstance(item.value.get("title"), str)
    }
    understanding = result.get("understanding")
    understanding_needs = [
        getattr(need, "value", str(need))
        for need in getattr(understanding, "needs", [])
    ]
    parsed_entities = [
        (
            entity.as_dict()
            if hasattr(entity, "as_dict")
            else {
                "entity_type": str(getattr(entity, "entity_type", "")),
                "source_text": str(getattr(entity, "source_text", "")),
                "value": getattr(entity, "value", None),
            }
        )
        for entity in result.get("parsed_entities", [])
    ]
    requirement = result.get("requirement_status")
    requirement_status = getattr(requirement, "value", requirement)
    if requirement_status is not None:
        requirement_status = str(requirement_status)
    oracle_path: dict[str, Any] | None = None
    oracle_paths = case.get("oracle_paths", [])
    if oracle_paths:
        oracle_path = next(
            (
                path
                for path in oracle_paths
                if path.get("understanding", []) == understanding_needs
            ),
            None,
        )
    rule_matches: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for message in result.get("messages", []):
        if not isinstance(message, ToolMessage):
            continue
        content = message.content
        try:
            payload = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError:
            tool_results.append(
                {
                    "tool": message.name,
                    "success": None,
                    "code": "INVALID_JSON",
                }
            )
            continue
        if not isinstance(payload, dict):
            continue
        tool_results.append(
            {
                "tool": message.name,
                "success": payload.get("success"),
                "code": payload.get("code"),
            }
        )
        if message.name != "search_group_buy_rules":
            continue
        data = payload.get("data")
        matches = data.get("matches") if isinstance(data, dict) else None
        if isinstance(matches, list):
            rule_matches.extend(
                item for item in matches if isinstance(item, dict)
            )

    reasons: list[str] = []
    if oracle_paths and oracle_path is None:
        allowed_understanding = [
            path.get("understanding", []) for path in oracle_paths
        ]
        reasons.append(
            "Understanding 不符合允许路径: "
            + json.dumps(allowed_understanding, ensure_ascii=False)
        )
    raw_outcome = result.get("outcome")
    try:
        outcome = AgentOutcome.model_validate(raw_outcome)
    except (TypeError, ValueError):
        outcome = None
        reasons.append("缺少合法 AgentOutcome")

    actual_action = outcome.kind.value if outcome is not None else "ERROR"
    allowed_actions = case.get("allowed_actions", [case["expected_action"]])
    if actual_action not in allowed_actions:
        reasons.append(
            f"action 允许 {'/'.join(allowed_actions)}，实际 {actual_action}"
        )

    required_tools = (
        oracle_path.get("required_tools", [])
        if oracle_path is not None
        else case.get("required_tools", [])
    )
    missing_tools = [name for name in required_tools if name not in tool_calls]
    if missing_tools:
        reasons.append(f"缺少 Tool: {', '.join(missing_tools)}")

    forbidden_tools = [
        name for name in case.get("forbidden_tools", []) if name in tool_calls
    ]
    if forbidden_tools:
        reasons.append(f"调用禁用 Tool: {', '.join(forbidden_tools)}")

    missing_tool_calls = [
        expected
        for expected in case.get("required_tool_calls", [])
        if not any(
            call["name"] == expected["name"]
            and call.get("args", {}) == expected.get("args", {})
            for call in tool_call_records
        )
    ]
    if missing_tool_calls:
        reasons.append(
            "缺少精确 Tool 调用: "
            + json.dumps(missing_tool_calls, ensure_ascii=False)
        )

    missing_evidence = [
        pattern
        for pattern in case.get("required_evidence", [])
        if not has_path(pattern, evidence_paths)
    ]
    if missing_evidence:
        reasons.append(f"缺少 Evidence: {', '.join(missing_evidence)}")

    forbidden_evidence = [
        pattern
        for pattern in case.get("forbidden_evidence", [])
        if has_path(pattern, evidence_paths)
    ]
    if forbidden_evidence:
        reasons.append(f"出现禁用 Evidence: {', '.join(forbidden_evidence)}")

    if (
        oracle_path is not None
        and "expected_rule_matches" in oracle_path
        and rule_matches != oracle_path["expected_rule_matches"]
    ):
        reasons.append(
            "Rule matches 不符合预期: "
            + json.dumps(rule_matches, ensure_ascii=False)
        )

    if outcome is not None:
        expected_final = "\n".join(claim.text for claim in outcome.claims)
        if outcome.final_answer != expected_final:
            reasons.append("final_answer 与 validated claims 不一致")

        claim_text = "\n".join(claim.text for claim in outcome.claims).lower()
        forbidden_claims = [
            term
            for term in case["forbidden_claim_terms"]
            if term.lower() in claim_text
        ]
        if forbidden_claims:
            reasons.append(f"命中禁用 Claim: {', '.join(forbidden_claims)}")

        for claim in outcome.claims:
            missing_paths = [
                path for path in claim.evidence if path not in evidence_types
            ]
            if missing_paths:
                reasons.append(
                    f"Claim 引用不存在 Evidence: {', '.join(missing_paths)}"
                )
            if claim.type in {ClaimType.FACT, ClaimType.RULE}:
                if not claim.evidence:
                    reasons.append(f"{claim.type.value} Claim 缺少 Evidence")
                    continue
                expected_type = (
                    EvidenceType.FACT
                    if claim.type is ClaimType.FACT
                    else EvidenceType.RULE
                )
                wrong_type = [
                    path
                    for path in claim.evidence
                    if path in evidence_types
                    and evidence_types[path] is not expected_type
                ]
                if wrong_type:
                    reasons.append(
                        f"{claim.type.value} Claim Evidence 类型错误: "
                        f"{', '.join(wrong_type)}"
                    )

        rule_claims = [
            claim for claim in outcome.claims if claim.type is ClaimType.RULE
        ]
        claimed_rule_ids = {
            claim.rule_id for claim in rule_claims if claim.rule_id
        }
        required_rule_ids = set(case.get("required_rule_ids", []))
        missing_rule_ids = sorted(required_rule_ids.difference(claimed_rule_ids))
        if missing_rule_ids:
            reasons.append(
                f"RULE Claim 缺少要求 rule_id: {', '.join(missing_rule_ids)}"
            )
        required_rule_ids_any = set(case.get("required_rule_ids_any", []))
        if required_rule_ids_any and not required_rule_ids_any.intersection(
            claimed_rule_ids
        ):
            reasons.append(
                "RULE Claim 未命中任一允许 rule_id: "
                + ", ".join(sorted(required_rule_ids_any))
            )
        forbidden_rule_ids = sorted(
            set(case.get("forbidden_rule_ids", [])).intersection(
                claimed_rule_ids
            )
        )
        if forbidden_rule_ids:
            reasons.append(
                f"RULE Claim 命中禁用 rule_id: {', '.join(forbidden_rule_ids)}"
            )

        for claim in rule_claims:
            if not claim.rule_id:
                reasons.append("RULE Claim 缺少 rule_id")
                continue
            match = current_rule_matches.get(claim.rule_id)
            if match is None:
                reasons.append(
                    f"RULE Claim rule_id 不在本轮 Tool match: {claim.rule_id}"
                )
                continue
            if match["path"] not in claim.evidence:
                reasons.append(
                    "RULE Claim 未引用本轮对应 match Evidence: "
                    f"{claim.rule_id}"
                )
            citation = f"依据《{match['title']}》"
            if citation not in claim.text or citation not in outcome.final_answer:
                reasons.append(
                    f"最终回答缺少真实标题引用: {claim.rule_id}"
                )
    else:
        rule_claims = []

    expected_agent_rounds = case.get("expected_agent_rounds")
    if (
        expected_agent_rounds is not None
        and result.get("agent_rounds") != expected_agent_rounds
    ):
        reasons.append(
            "agent_rounds 期望 "
            f"{expected_agent_rounds}，实际 {result.get('agent_rounds')}"
        )

    stage = _failure_stage(
        case=case,
        reasons=reasons,
        actual_action=actual_action,
        tools=tool_calls,
        tool_results=tool_results,
        evidence_paths=evidence_paths,
        understanding=understanding_needs,
        parsed_entities=parsed_entities,
        requirement_status=requirement_status,
        rule_matches=rule_matches,
    )
    return EvalRun(
        passed=not reasons,
        actual_action=actual_action,
        tools=tool_calls,
        evidence_paths=evidence_paths,
        understanding=understanding_needs,
        rule_queries=rule_queries,
        rule_matches=rule_matches,
        rule_claims=[claim.model_dump(mode="json") for claim in rule_claims],
        final_answer=outcome.final_answer if outcome is not None else "",
        input_tokens=token_usage.input_tokens,
        output_tokens=token_usage.output_tokens,
        token_count=token_usage.total_tokens or 0,
        latency_seconds=latency_seconds,
        reasons=reasons,
        failure_stage=stage,
        parsed_entities=parsed_entities,
        requirement_status=requirement_status,
        tool_results=tool_results,
    )


def wilson_interval(passed: int, total: int) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    proportion = passed / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / total
            + z * z / (4 * total * total)
        )
        / denominator
    )
    return center - margin, center + margin


def print_case_results(results: list[CaseRuns]) -> None:
    headers = ("ID", "CATEGORY", "RUNS", "STATUS", "AVG TOKENS", "AVG SEC", "REASON")
    rows: list[tuple[str, ...]] = []
    for item in results:
        passed = sum(run.passed for run in item.runs)
        expected_failure = bool(item.case.get("expected_failure"))
        if expected_failure:
            status = "XPASS" if passed == RUNS_PER_CASE else "XFAIL"
        elif passed == RUNS_PER_CASE:
            status = "PASS"
        elif passed == 0:
            status = "FAIL"
        else:
            status = "FLAKY"
        reasons = []
        for index, run in enumerate(item.runs, start=1):
            if run.reasons:
                reasons.append(f"r{index}: {'; '.join(run.reasons)}")
        rows.append(
            (
                item.case["id"],
                item.case["category"],
                f"{passed}/{RUNS_PER_CASE}",
                status,
                f"{sum(run.token_count for run in item.runs) / RUNS_PER_CASE:.0f}",
                f"{sum(run.latency_seconds for run in item.runs) / RUNS_PER_CASE:.2f}",
                " | ".join(reasons) or "-",
            )
        )

    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def print_metrics(results: list[CaseRuns]) -> None:
    current = [item for item in results if not item.case.get("expected_failure")]
    expected_failures = [item for item in results if item.case.get("expected_failure")]
    current_runs = [run for item in current for run in item.runs]
    run_passes = sum(run.passed for run in current_runs)
    all_passes = sum(all(run.passed for run in item.runs) for item in current)
    run_interval = wilson_interval(run_passes, len(current_runs))
    all_interval = wilson_interval(all_passes, len(current))
    average_tokens = sum(run.token_count for run in current_runs) / len(current_runs)
    average_latency = sum(run.latency_seconds for run in current_runs) / len(current_runs)
    expected_passes = sum(
        run.passed for item in expected_failures for run in item.runs
    )

    print()
    print(f"CASES total={len(results)} current={len(current)} expected_failure={len(expected_failures)}")
    print(
        "SINGLE_RUN_PASS "
        f"{run_passes}/{len(current_runs)} ({run_passes / len(current_runs):.2%})"
    )
    print(
        "SINGLE_RUN_WILSON_95 "
        f"[{run_interval[0]:.2%}, {run_interval[1]:.2%}]"
    )
    print(
        "THREE_RUN_ALL_PASS "
        f"{all_passes}/{len(current)} ({all_passes / len(current):.2%})"
    )
    print(
        "THREE_RUN_WILSON_95 "
        f"[{all_interval[0]:.2%}, {all_interval[1]:.2%}]"
    )
    print(f"AVERAGE_TOKENS {average_tokens:.2f}")
    print(f"AVERAGE_LATENCY_SECONDS {average_latency:.3f}")
    print(
        "EXPECTED_FAILURE_RUNS "
        f"{expected_passes}/{len(expected_failures) * RUNS_PER_CASE} passed"
    )
    print("CATEGORY_COUNTS " + json.dumps(Counter(item.case["category"] for item in results), ensure_ascii=False))


def _git_text(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _evaluation_summary(results: list[CaseRuns]) -> dict[str, Any]:
    runs = [run for item in results for run in item.runs]
    passed_runs = sum(run.passed for run in runs)
    run_interval = wilson_interval(passed_runs, len(runs))
    pass_distribution = Counter(
        sum(run.passed for run in item.runs) for item in results
    )
    stable_cases = pass_distribution[RUNS_PER_CASE]

    category_rows: list[dict[str, Any]] = []
    for category in dict.fromkeys(item.case["category"] for item in results):
        category_cases = [
            item for item in results if item.case["category"] == category
        ]
        category_runs = [run for item in category_cases for run in item.runs]
        category_passes = sum(run.passed for run in category_runs)
        category_rows.append(
            {
                "category": category,
                "cases": len(category_cases),
                "runs": len(category_runs),
                "passed": category_passes,
                "pass_rate": category_passes / len(category_runs),
            }
        )

    stage_runs: Counter[str] = Counter()
    stage_cases: dict[str, set[str]] = defaultdict(set)
    for item in results:
        for run in item.runs:
            if run.passed:
                continue
            stage = run.failure_stage or "OTHER"
            stage_runs[stage] += 1
            stage_cases[stage].add(str(item.case["id"]))
    failure_stage_rows = [
        {
            "failure_stage": stage,
            "failed_runs": stage_runs[stage],
            "affected_cases": len(stage_cases[stage]),
        }
        for stage in FAILURE_STAGES
        if stage_runs[stage]
    ]
    failure_stage_rows.sort(
        key=lambda row: (-row["failed_runs"], row["failure_stage"])
    )

    token_breakdown_available = all(
        run.input_tokens is not None and run.output_tokens is not None
        for run in runs
    )
    total_tokens_available = all(
        run.input_tokens is not None
        and run.output_tokens is not None
        and run.token_count == run.input_tokens + run.output_tokens
        for run in runs
    )
    token_usage: dict[str, Any]
    if token_breakdown_available and total_tokens_available:
        total_input = sum(run.input_tokens or 0 for run in runs)
        total_output = sum(run.output_tokens or 0 for run in runs)
        token_usage = {
            "status": "available",
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
        }
    else:
        token_usage = {
            "status": "unavailable",
            "reason": "完整 input/output usage metadata 未覆盖全部运行",
        }

    affected = [
        item for item in results if not all(run.passed for run in item.runs)
    ]
    return {
        "passed_runs": passed_runs,
        "total_runs": len(runs),
        "single_run_pass_rate": passed_runs / len(runs),
        "single_run_wilson_95": {
            "lower": run_interval[0],
            "upper": run_interval[1],
        },
        "stable_cases": stable_cases,
        "total_cases": len(results),
        "stable_case_rate": stable_cases / len(results),
        "case_pass_distribution": {
            f"{passed}/{RUNS_PER_CASE}": pass_distribution[passed]
            for passed in range(RUNS_PER_CASE, -1, -1)
        },
        "stability": {
            "cases_with_at_least_one_failure": len(affected),
            "cases_failed_one_of_three": pass_distribution[2],
            "cases_failed_at_least_two_of_three": (
                pass_distribution[1] + pass_distribution[0]
            ),
            "cases_failed_three_of_three": pass_distribution[0],
        },
        "categories": category_rows,
        "failure_stages": failure_stage_rows,
        "token_usage": token_usage,
    }


def _machine_runs(results: list[CaseRuns]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in results:
        for run_index, run in enumerate(item.runs, start=1):
            records.append(
                {
                    "case_id": item.case["id"],
                    "category": item.case["category"],
                    "run": run_index,
                    "passed": run.passed,
                    "expected_outcome": item.case["expected_action"],
                    "actual_outcome": run.actual_action,
                    "failure_stage": run.failure_stage,
                    "reasons": run.reasons,
                    "understanding": run.understanding,
                    "parsed_entities": run.parsed_entities,
                    "requirement_status": run.requirement_status,
                    "tools": run.tools,
                    "tool_results": run.tool_results,
                    "evidence_paths": run.evidence_paths,
                    "rule_queries": run.rule_queries,
                    "rule_matches": [
                        {
                            "rule_id": match.get("rule_id"),
                            "rerank_score": match.get("rerank_score"),
                        }
                        for match in run.rule_matches
                    ],
                    "rule_claims": run.rule_claims,
                    "latency_seconds": run.latency_seconds,
                    "tokens": {
                        "input": run.input_tokens,
                        "output": run.output_tokens,
                        "total": (
                            run.token_count
                            if run.input_tokens is not None
                            and run.output_tokens is not None
                            else None
                        ),
                    },
                }
            )
    return records


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _write_final_artifacts(
    results: list[CaseRuns],
    metadata: dict[str, Any],
) -> None:
    summary = _evaluation_summary(results)
    machine_payload = {
        "metadata": metadata,
        "summary": summary,
        "runs": _machine_runs(results),
    }
    FINAL_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINAL_RESULTS_PATH.write_text(
        json.dumps(machine_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    distribution = summary["case_pass_distribution"]
    token_usage = summary["token_usage"]
    lines = [
        "# E1 Final Evaluation — 69 × 3",
        "",
        "## Run metadata",
        "",
        f"- Git commit: `{metadata['git_commit']}`",
        f"- Branch: `{metadata['git_branch']}`",
        f"- Model: `{metadata['model']}`",
        f"- Started: `{metadata['started_at']}`",
        f"- Completed: `{metadata['completed_at']}`",
        f"- Total elapsed: `{metadata['elapsed_seconds']:.3f}s`",
        f"- Parameters: `EVAL_RUNS_PER_CASE={RUNS_PER_CASE}`; sequential runs; independent thread IDs",
        f"- Working tree dirty at start: `{str(metadata['working_tree_dirty']).lower()}` (evaluation/report instrumentation only)",
        "",
        "## Core metrics",
        "",
        (
            f"- Single-run pass rate: **{summary['passed_runs']}/{summary['total_runs']} "
            f"({summary['single_run_pass_rate']:.2%})**"
        ),
        (
            "- Wilson 95% confidence interval: "
            f"**[{summary['single_run_wilson_95']['lower']:.2%}, "
            f"{summary['single_run_wilson_95']['upper']:.2%}]**"
        ),
        (
            f"- Case-level stable pass rate: **{summary['stable_cases']}/"
            f"{summary['total_cases']} ({summary['stable_case_rate']:.2%})**"
        ),
        "",
        "| Case result | Cases |",
        "|---|---:|",
        f"| 3/3 PASS | {distribution['3/3']} |",
        f"| 2/3 PASS | {distribution['2/3']} |",
        f"| 1/3 PASS | {distribution['1/3']} |",
        f"| 0/3 PASS | {distribution['0/3']} |",
        "",
        "## Category statistics",
        "",
        "| Category | Cases | Runs | Pass | Single-run Pass Rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in summary["categories"]:
        lines.append(
            f"| {_markdown_cell(row['category'])} | {row['cases']} | "
            f"{row['runs']} | {row['passed']} | {row['pass_rate']:.2%} |"
        )

    stability = summary["stability"]
    lines.extend(
        [
            "",
            "## Stability summary",
            "",
            "| Metric | Cases |",
            "|---|---:|",
            f"| At least one failed run | {stability['cases_with_at_least_one_failure']} |",
            f"| Failed exactly 1/3 | {stability['cases_failed_one_of_three']} |",
            f"| Failed at least 2/3 | {stability['cases_failed_at_least_two_of_three']} |",
            f"| Failed 3/3 | {stability['cases_failed_three_of_three']} |",
            "",
            "## Failure-stage aggregation",
            "",
            "| Failure Stage | Failed Runs | Affected Cases |",
            "|---|---:|---:|",
        ]
    )
    if summary["failure_stages"]:
        for row in summary["failure_stages"]:
            lines.append(
                f"| {row['failure_stage']} | {row['failed_runs']} | "
                f"{row['affected_cases']} |"
            )
    else:
        lines.append("| — | 0 | 0 |")

    failed_runs = [
        (item, run_index, run)
        for item in results
        for run_index, run in enumerate(item.runs, start=1)
        if not run.passed
    ]
    lines.extend(
        [
            "",
            "## Failed runs",
            "",
            "| case_id | Run | Expected | Actual | Failure Stage | Reason |",
            "|---|---:|---|---|---|---|",
        ]
    )
    if failed_runs:
        for item, run_index, run in failed_runs:
            lines.append(
                f"| `{item.case['id']}` | {run_index} | "
                f"{item.case['expected_action']} | {run.actual_action} | "
                f"{run.failure_stage or 'OTHER'} | "
                f"{_markdown_cell('; '.join(run.reasons))} |"
            )
    else:
        lines.append("| — | — | — | — | — | No failed runs |")

    lines.extend(
        [
            "",
            "## Failed-case summary",
            "",
            "| case_id | Failures / 3 | Stage | Summary |",
            "|---|---:|---|---|",
        ]
    )
    affected_items = [
        item for item in results if any(not run.passed for run in item.runs)
    ]
    if affected_items:
        for item in affected_items:
            failed = [run for run in item.runs if not run.passed]
            stages = ", ".join(
                dict.fromkeys(run.failure_stage or "OTHER" for run in failed)
            )
            reasons = "; ".join(
                dict.fromkeys(reason for run in failed for reason in run.reasons)
            )
            lines.append(
                f"| `{item.case['id']}` | {len(failed)}/3 | {stages} | "
                f"{_markdown_cell(reasons)} |"
            )
    else:
        lines.append("| — | 0/3 | — | No affected cases |")

    lines.extend(["", "## Token usage", ""])
    if token_usage["status"] == "available":
        lines.extend(
            [
                f"- Input tokens: **{token_usage['input_tokens']}**",
                f"- Output tokens: **{token_usage['output_tokens']}**",
                f"- Total tokens: **{token_usage['total_tokens']}**",
            ]
        )
    else:
        lines.append(f"- **unavailable** — {token_usage['reason']}")
    lines.extend(
        [
            "",
            f"Machine-readable results: `{FINAL_RESULTS_PATH.relative_to(PROJECT_ROOT).as_posix()}`",
            "",
        ]
    )
    FINAL_REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    cases = select_cases(load_cases())
    diagnostic_mode = bool(os.getenv("EVAL_CASE_IDS", "").strip())
    final_report_mode = os.getenv("EVAL_FINAL_REPORT", "").strip() == "1"
    if final_report_mode and (
        len(cases) != 69 or RUNS_PER_CASE != 3 or diagnostic_mode
    ):
        raise ValueError("最终评测必须是完整 69 Case、每条 3 次且不筛选")
    suite_started_at = datetime.now().astimezone()
    suite_started = time.perf_counter()
    git_commit = _git_text("rev-parse", "HEAD")
    git_branch = _git_text("branch", "--show-current")
    working_tree_dirty = bool(_git_text("status", "--porcelain"))
    port = free_port()
    fake_java = start_fake_java(port)
    original_base_url = os.environ.get("FAKE_JAVA_BASE_URL")
    original_java_base_url = os.environ.get("JAVA_BASE_URL")
    checkpoint_directory = tempfile.TemporaryDirectory(
        prefix="group-buy-agent-eval-"
    )
    agent: Any | None = None

    try:
        from agent import close_order_agent, create_order_agent
        from tools.context import AgentContext

        # agent loads .env at import time; this runner deliberately evaluates
        # against its isolated fake service.
        os.environ["JAVA_BASE_URL"] = ""

        agent = create_order_agent(
            Path(checkpoint_directory.name) / "checkpoints.sqlite"
        )
        results: list[CaseRuns] = []
        for case in cases:
            runs: list[EvalRun] = []
            for _ in range(RUNS_PER_CASE):
                os.environ["FAKE_JAVA_BASE_URL"] = (
                    "http://127.0.0.1:1"
                    if case.get("service_unavailable")
                    else f"http://127.0.0.1:{port}"
                )
                started_at = time.perf_counter()
                try:
                    result, token_usage, latency = invoke_case(
                        agent,
                        AgentContext,
                        case,
                        session_id=f"eval-{case['id']}-{uuid.uuid4().hex}",
                    )
                    runs.append(
                        evaluate_result(
                            result,
                            case,
                            token_usage,
                            latency,
                        )
                    )
                except Exception as error:
                    runs.append(
                        EvalRun(
                            passed=False,
                            actual_action="ERROR",
                            tools=[],
                            evidence_paths=[],
                            understanding=[],
                            rule_queries=[],
                            rule_matches=[],
                            rule_claims=[],
                            final_answer="",
                            input_tokens=None,
                            output_tokens=None,
                            token_count=0,
                            latency_seconds=time.perf_counter() - started_at,
                            reasons=[f"运行异常: {type(error).__name__}"],
                            failure_stage="OTHER",
                            parsed_entities=[],
                            requirement_status=None,
                            tool_results=[],
                        )
                    )
            results.append(CaseRuns(case=case, runs=runs))
            passed = sum(run.passed for run in runs)
            print(f"[{len(results):02d}/{len(cases)}] {case['id']}: {passed}/{RUNS_PER_CASE}", flush=True)
            if diagnostic_mode:
                for run_number, run in enumerate(runs, start=1):
                    print(
                        "DIAGNOSTIC "
                        + json.dumps(
                            {
                                "case_id": case["id"],
                                "run": run_number,
                                "understanding": run.understanding,
                                "query": run.rule_queries,
                                "user_text": (
                                    case.get("input")
                                    or case.get("turns", [""])[-1]
                                ),
                                "search_called": (
                                    "search_group_buy_rules" in run.tools
                                ),
                                "matches": run.rule_matches,
                                "rule_claims": run.rule_claims,
                                "outcome": run.actual_action,
                                "final_answer": run.final_answer,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )

        print()
        print_case_results(results)
        print_metrics(results)
        if final_report_mode:
            suite_completed_at = datetime.now().astimezone()
            metadata = {
                "git_commit": git_commit,
                "git_branch": git_branch,
                "working_tree_dirty": working_tree_dirty,
                "model": os.getenv("OPENAI_MODEL", "unavailable"),
                "started_at": suite_started_at.isoformat(timespec="seconds"),
                "completed_at": suite_completed_at.isoformat(
                    timespec="seconds"
                ),
                "elapsed_seconds": time.perf_counter() - suite_started,
                "runs_per_case": RUNS_PER_CASE,
                "total_cases": len(cases),
                "total_runs": len(cases) * RUNS_PER_CASE,
                "execution": "sequential; independent session/thread per run",
            }
            _write_final_artifacts(results, metadata)
            print(f"FINAL_RESULTS {FINAL_RESULTS_PATH}")
            print(f"FINAL_REPORT {FINAL_REPORT_PATH}")
    finally:
        if agent is not None:
            close_order_agent(agent)
        checkpoint_directory.cleanup()
        if original_base_url is None:
            os.environ.pop("FAKE_JAVA_BASE_URL", None)
        else:
            os.environ["FAKE_JAVA_BASE_URL"] = original_base_url
        if original_java_base_url is None:
            os.environ.pop("JAVA_BASE_URL", None)
        else:
            os.environ["JAVA_BASE_URL"] = original_java_base_url
        fake_java.terminate()
        try:
            fake_java.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake_java.kill()
            fake_java.wait(timeout=5)


if __name__ == "__main__":
    main()
