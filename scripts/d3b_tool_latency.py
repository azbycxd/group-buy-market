from __future__ import annotations

import argparse
import json
import statistics
import time
from types import SimpleNamespace

from rag.pipeline import warmup_rule_search_pipeline
from tools.context import AgentContext
from tools.rule_search import search_group_buy_rules


CASES = (
    ("钱已经付了，但团没凑齐，能直接退吗？", "已支付未成团退款规则"),
    ("这单关了还能补钱恢复吗？", "关闭订单重新支付规则"),
    ("待支付、已完成和已关闭是什么意思？", "订单状态含义"),
    ("订单关闭后还能再次申请退款吗？", "关闭订单退款提议规则"),
    ("拼团商品退货运费谁出？", "拼团退货运费规则"),
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def main() -> None:
    parser = argparse.ArgumentParser(
        description="测量 warm search_group_buy_rules Tool 内部延迟"
    )
    parser.add_argument("--calls", type=int, default=20)
    arguments = parser.parse_args()
    if arguments.calls < 20:
        raise ValueError("D3b Tool latency 至少测量 20 次")

    warmup_rule_search_pipeline()
    durations: list[float] = []
    for index in range(arguments.calls):
        original, model_query = CASES[index % len(CASES)]
        runtime = SimpleNamespace(
            context=AgentContext(
                user_id="d3b-latency-user",
                user_text=original,
            )
        )
        started = time.perf_counter()
        search_group_buy_rules.func(query=model_query, runtime=runtime)
        durations.append((time.perf_counter() - started) * 1000)

    print(
        json.dumps(
            {
                "calls": len(durations),
                "mean_ms": statistics.fmean(durations),
                "p50_ms": _percentile(durations, 0.50),
                "p95_ms": _percentile(durations, 0.95),
                "durations_ms": durations,
                "scope": "search_group_buy_rules entry to ToolResult return",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
