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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import LLMResult

from evidence import EvidenceType
from outcome import AgentOutcome, ClaimType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).with_name("cases.yaml")
VALID_ACTIONS = {"ANSWER", "REQUEST_INPUT", "HANDOFF"}
RUNS_PER_CASE = 3


@dataclass
class EvalRun:
    passed: bool
    actual_action: str
    tools: list[str]
    evidence_paths: list[str]
    token_count: int
    latency_seconds: float
    reasons: list[str]


@dataclass
class CaseRuns:
    case: dict[str, Any]
    runs: list[EvalRun]


class TokenCounter(BaseCallbackHandler):
    def __init__(self) -> None:
        self.total_tokens = 0
        self._lock = threading.Lock()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        message_total = 0
        for generation_list in response.generations:
            for generation in generation_list:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if isinstance(usage, dict):
                    total = usage.get("total_tokens")
                    if isinstance(total, int):
                        message_total += total

        if message_total == 0 and isinstance(response.llm_output, dict):
            usage = response.llm_output.get("token_usage") or response.llm_output.get(
                "usage"
            )
            if isinstance(usage, dict):
                total = usage.get("total_tokens")
                if isinstance(total, int):
                    message_total = total

        with self._lock:
            self.total_tokens += message_total


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
) -> tuple[dict[str, Any], int, float]:
    counter = TokenCounter()
    context = context_type(user_id=case.get("user_id", "demo-user"))
    turns = case.get("turns") or [case["input"]]
    session_id = session_id or f"eval-{case['id']}-{uuid.uuid4().hex}"
    result: dict[str, Any] = {}
    started_at = time.perf_counter()

    for turn in turns:
        result = agent.invoke(
            {"messages": [HumanMessage(content=turn)]},
            context=context,
            config={
                "callbacks": [counter],
                "configurable": {"thread_id": session_id},
            },
        )

    return result, counter.total_tokens, time.perf_counter() - started_at


def evaluate_result(
    result: dict[str, Any],
    case: dict[str, Any],
    token_count: int,
    latency_seconds: float,
) -> EvalRun:
    tool_calls = [
        call["name"]
        for message in result.get("messages", [])
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    evidence = result.get("evidence", [])
    evidence_paths = [item.path for item in evidence]
    evidence_types = {item.path: item.type for item in evidence}

    reasons: list[str] = []
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

    missing_tools = [
        name for name in case.get("required_tools", []) if name not in tool_calls
    ]
    if missing_tools:
        reasons.append(f"缺少 Tool: {', '.join(missing_tools)}")

    forbidden_tools = [
        name for name in case.get("forbidden_tools", []) if name in tool_calls
    ]
    if forbidden_tools:
        reasons.append(f"调用禁用 Tool: {', '.join(forbidden_tools)}")

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

    return EvalRun(
        passed=not reasons,
        actual_action=actual_action,
        tools=tool_calls,
        evidence_paths=evidence_paths,
        token_count=token_count,
        latency_seconds=latency_seconds,
        reasons=reasons,
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


def main() -> None:
    cases = load_cases()
    port = free_port()
    fake_java = start_fake_java(port)
    original_base_url = os.environ.get("FAKE_JAVA_BASE_URL")
    checkpoint_directory = tempfile.TemporaryDirectory(
        prefix="group-buy-agent-eval-"
    )
    agent: Any | None = None

    try:
        from agent import close_order_agent, create_order_agent
        from tools.context import AgentContext

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
                    result, tokens, latency = invoke_case(
                        agent,
                        AgentContext,
                        case,
                        session_id=f"eval-{case['id']}-{uuid.uuid4().hex}",
                    )
                    runs.append(evaluate_result(result, case, tokens, latency))
                except Exception as error:
                    runs.append(
                        EvalRun(
                            passed=False,
                            actual_action="ERROR",
                            tools=[],
                            evidence_paths=[],
                            token_count=0,
                            latency_seconds=time.perf_counter() - started_at,
                            reasons=[f"运行异常: {type(error).__name__}"],
                        )
                    )
            results.append(CaseRuns(case=case, runs=runs))
            passed = sum(run.passed for run in runs)
            print(f"[{len(results):02d}/{len(cases)}] {case['id']}: {passed}/{RUNS_PER_CASE}", flush=True)

        print()
        print_case_results(results)
        print_metrics(results)
    finally:
        if agent is not None:
            close_order_agent(agent)
        checkpoint_directory.cleanup()
        if original_base_url is None:
            os.environ.pop("FAKE_JAVA_BASE_URL", None)
        else:
            os.environ["FAKE_JAVA_BASE_URL"] = original_base_url
        fake_java.terminate()
        try:
            fake_java.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake_java.kill()
            fake_java.wait(timeout=5)


if __name__ == "__main__":
    main()
