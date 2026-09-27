from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import AIMessage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).with_name("cases.yaml")
VALID_ACTIONS = {"ANSWER", "REQUEST_INPUT", "HANDOFF"}


@dataclass
class EvalResult:
    case_id: str
    expected_action: str
    actual_action: str
    tools: list[str]
    answer: str
    passed: bool
    reasons: list[str]


def load_cases() -> list[dict[str, Any]]:
    with CASES_PATH.open("r", encoding="utf-8") as file:
        cases = yaml.safe_load(file)
    if not isinstance(cases, list) or len(cases) != 16:
        raise ValueError("evals/cases.yaml 必须包含 16 条 Case")
    for case in cases:
        required = {
            "id",
            "input",
            "expected_action",
            "required_tools",
            "forbidden_answer_terms",
        }
        missing = required.difference(case)
        if missing:
            raise ValueError(f"Case {case.get('id')} 缺少字段: {sorted(missing)}")
        if case["expected_action"] not in VALID_ACTIONS:
            raise ValueError(f"Case {case['id']} expected_action 非法")
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


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def infer_action(answer: str, tools: list[str]) -> str:
    normalized = answer.lower()
    handoff_markers = (
        "转人工",
        "人工客服",
        "联系平台客服",
        "联系官方客服",
        "人工处理",
    )
    request_input_markers = (
        "请提供",
        "请告知",
        "需要您提供",
        "需要提供",
        "活动 id",
        "活动id",
    )
    if any(marker in normalized for marker in handoff_markers):
        return "HANDOFF"
    if not tools and any(marker in normalized for marker in request_input_markers):
        return "REQUEST_INPUT"
    return "ANSWER"


def evaluate_case(agent: Any, context_type: Any, case: dict[str, Any]) -> EvalResult:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": case["input"]}]},
        context=context_type(user_id=case.get("user_id", "demo-user")),
    )
    tool_calls: list[str] = []
    final_answer = ""
    for message in result["messages"]:
        if isinstance(message, AIMessage):
            tool_calls.extend(call["name"] for call in message.tool_calls)
            if not message.tool_calls and message.content:
                final_answer = message_text(message.content)

    actual_action = infer_action(final_answer, tool_calls)
    reasons: list[str] = []
    if actual_action != case["expected_action"]:
        reasons.append(
            f"action 期望 {case['expected_action']}，实际 {actual_action}"
        )

    missing_tools = [
        name for name in case["required_tools"] if name not in tool_calls
    ]
    if missing_tools:
        reasons.append(f"缺少 Tool: {', '.join(missing_tools)}")

    forbidden_tools = [
        name for name in case.get("forbidden_tools", []) if name in tool_calls
    ]
    if forbidden_tools:
        reasons.append(f"调用禁用 Tool: {', '.join(forbidden_tools)}")

    normalized_answer = final_answer.lower()
    forbidden_hits = [
        term
        for term in case["forbidden_answer_terms"]
        if term.lower() in normalized_answer
    ]
    if forbidden_hits:
        reasons.append(f"命中禁用词: {', '.join(forbidden_hits)}")

    return EvalResult(
        case_id=case["id"],
        expected_action=case["expected_action"],
        actual_action=actual_action,
        tools=tool_calls,
        answer=final_answer,
        passed=not reasons,
        reasons=reasons,
    )


def print_results(results: list[EvalResult]) -> None:
    headers = ("ID", "EXPECTED", "ACTUAL", "TOOLS", "RESULT", "REASON")
    rows = []
    for result in results:
        rows.append(
            (
                result.case_id,
                result.expected_action,
                result.actual_action,
                ",".join(result.tools) or "-",
                "PASS" if result.passed else "FAIL",
                "; ".join(result.reasons) or "-",
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
    passed = sum(result.passed for result in results)
    print(f"PASS {passed}/{len(results)}")


def main() -> None:
    cases = load_cases()
    port = free_port()
    fake_java = start_fake_java(port)
    original_base_url = os.environ.get("FAKE_JAVA_BASE_URL")
    os.environ["FAKE_JAVA_BASE_URL"] = f"http://127.0.0.1:{port}"

    try:
        from agent import create_order_agent
        from tools.context import AgentContext

        agent = create_order_agent()
        results: list[EvalResult] = []
        for case in cases:
            if case.get("service_unavailable"):
                os.environ["FAKE_JAVA_BASE_URL"] = "http://127.0.0.1:1"
            else:
                os.environ["FAKE_JAVA_BASE_URL"] = f"http://127.0.0.1:{port}"
            try:
                results.append(evaluate_case(agent, AgentContext, case))
            except Exception as error:
                results.append(
                    EvalResult(
                        case_id=case["id"],
                        expected_action=case["expected_action"],
                        actual_action="ERROR",
                        tools=[],
                        answer="",
                        passed=False,
                        reasons=[f"运行异常: {type(error).__name__}"],
                    )
                )
        print_results(results)
    finally:
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
