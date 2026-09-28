from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from agent import close_order_agent, create_order_agent
from tools.context import AgentContext
from tools.rule_search import search_group_buy_rules


MANUAL_CASES = (
    (
        "hard_08",
        "钱已经付了，但团没凑齐，能直接退吗？",
        "已支付但未成团时的退款规则",
    ),
    (
        "paid_formed_refund",
        "钱已经付了而且团也成了，退款需要人工审核吗？",
        "已支付已成团退款审核规则",
    ),
    (
        "close_refund_arrival",
        "订单 CLOSE 是否代表退款资金已经到账？",
        "订单关闭与退款到账规则",
    ),
    (
        "shipping",
        "拼团商品包邮吗？",
        "拼团包邮规则",
    ),
    (
        "invoice",
        "拼团购买后能开发票吗？",
        "拼团发票规则",
    ),
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _tool_call(original: str, query: str) -> dict[str, Any]:
    return search_group_buy_rules.func(
        query=query,
        runtime=SimpleNamespace(
            context=AgentContext(
                user_id="d3a-eval-user",
                user_text=original,
            )
        ),
    )


def runtime_retrieval_eval(repeats: int) -> dict[str, Any]:
    durations: list[float] = []
    first_results: dict[str, dict[str, Any]] = {}
    for repeat in range(repeats):
        for case_id, original, query in MANUAL_CASES:
            started = time.perf_counter()
            result = _tool_call(original, query)
            durations.append((time.perf_counter() - started) * 1000)
            if repeat == 0:
                first_results[case_id] = {
                    "model_query": query,
                    "original_utterance": original,
                    "matches": result["data"]["matches"],
                }
    warm = durations[1:]
    return {
        "cold_ms": durations[0],
        "warm_calls": len(warm),
        "warm_mean_ms": statistics.fmean(warm),
        "warm_p50_ms": _percentile(warm, 0.50),
        "warm_p95_ms": _percentile(warm, 0.95),
        "retrievals": first_results,
    }


def _agent_observation(result: dict[str, Any]) -> dict[str, Any]:
    queries: list[str] = []
    matches: list[dict[str, Any]] = []
    for message in result.get("messages", []):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call.get("name") == "search_group_buy_rules":
                    query = call.get("args", {}).get("query")
                    if isinstance(query, str):
                        queries.append(query)
        elif isinstance(message, ToolMessage) and message.name == "search_group_buy_rules":
            try:
                payload = json.loads(str(message.content))
            except json.JSONDecodeError:
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            found = data.get("matches") if isinstance(data, dict) else None
            if isinstance(found, list):
                matches = [item for item in found if isinstance(item, dict)]
    outcome = result.get("outcome")
    kind = getattr(getattr(outcome, "kind", None), "value", None)
    if kind is None and isinstance(outcome, dict):
        kind = outcome.get("kind")
    return {
        "model_queries": queries,
        "matches": matches,
        "outcome": kind,
    }


def agent_eval() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="d3a-agent-eval-") as directory:
        agent = create_order_agent(Path(directory) / "checkpoints.sqlite")
        try:
            observations: dict[str, Any] = {}
            for case_id, original, _ in MANUAL_CASES:
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": original}]},
                    context=AgentContext(user_id="d3a-eval-user"),
                    config={
                        "configurable": {
                            "thread_id": f"d3a-{case_id}-{uuid.uuid4().hex}"
                        }
                    },
                )
                observations[case_id] = {
                    "original_utterance": original,
                    **_agent_observation(result),
                }
            return observations
        finally:
            close_order_agent(agent)


def main() -> None:
    parser = argparse.ArgumentParser(description="D3a runtime 检索与 Agent 人工检查")
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--skip-runtime", action="store_true")
    parser.add_argument("--skip-agent", action="store_true")
    arguments = parser.parse_args()
    if arguments.repeats < 1:
        raise ValueError("repeats 必须大于 0")

    output: dict[str, Any] = {}
    if not arguments.skip_runtime:
        output["runtime_retrieval"] = runtime_retrieval_eval(
            arguments.repeats
        )
    if not arguments.skip_agent:
        output["agent"] = agent_eval()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
