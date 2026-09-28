from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from rag.bm25 import BM25Index, SearchResult
from rag.chunking import DEFAULT_RULES_PATH, RuleChunk, load_rule_chunks


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = PROJECT_ROOT / "evals" / "rule_retrieval.yaml"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "docs" / "rag_eval.md"


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    case_id: str
    query: str
    gold_rule_ids: tuple[str, ...]

    @property
    def answerable(self) -> bool:
        return bool(self.gold_rule_ids)


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: RetrievalCase
    results: tuple[SearchResult, ...]
    first_gold_rank: int | None


def load_cases(path: str | Path) -> list[RetrievalCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("rule_retrieval.yaml 顶层必须是列表")
    cases: list[RetrievalCase] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("每条检索 Case 必须是对象")
        case_id = item.get("id")
        query = item.get("query")
        gold = item.get("gold_rule_ids")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"Case id 非法或重复: {case_id!r}")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"Case {case_id} query 非法")
        if not isinstance(gold, list) or not all(
            isinstance(rule_id, str) for rule_id in gold
        ):
            raise ValueError(f"Case {case_id} gold_rule_ids 非法")
        seen.add(case_id)
        cases.append(
            RetrievalCase(
                case_id=case_id,
                query=query.strip(),
                gold_rule_ids=tuple(gold),
            )
        )
    return cases


def evaluate(
    index: BM25Index,
    cases: list[RetrievalCase],
) -> list[CaseResult]:
    evaluated: list[CaseResult] = []
    for case in cases:
        results = tuple(index.search(case.query, top_k=len(index.chunks)))
        ranks = {
            result.rule_id: rank
            for rank, result in enumerate(results, start=1)
        }
        gold_ranks = [
            ranks[rule_id]
            for rule_id in case.gold_rule_ids
            if rule_id in ranks
        ]
        evaluated.append(
            CaseResult(
                case=case,
                results=results,
                first_gold_rank=min(gold_ranks) if gold_ranks else None,
            )
        )
    return evaluated


def _recall(results: list[CaseResult], at: int) -> float:
    return sum(
        result.first_gold_rank is not None and result.first_gold_rank <= at
        for result in results
    ) / len(results)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def score_distribution(values: list[float]) -> dict[str, float]:
    return {
        "count": float(len(values)),
        "min": min(values),
        "p25": _percentile(values, 0.25),
        "median": statistics.median(values),
        "p75": _percentile(values, 0.75),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def _metric_table(answerable: list[CaseResult]) -> list[str]:
    reciprocal_ranks = [
        0.0 if result.first_gold_rank is None else 1 / result.first_gold_rank
        for result in answerable
    ]
    return [
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| Recall@1 | {_recall(answerable, 1):.4f} |",
        f"| Recall@3 | {_recall(answerable, 3):.4f} |",
        f"| Recall@5 | {_recall(answerable, 5):.4f} |",
        f"| MRR | {statistics.fmean(reciprocal_ranks):.4f} |",
    ]


def _distribution_table(
    answerable_scores: list[float],
    unanswerable_scores: list[float],
) -> list[str]:
    answerable = score_distribution(answerable_scores)
    unanswerable = score_distribution(unanswerable_scores)
    lines = [
        "| 类型 | count | min | p25 | median | p75 | max | mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, values in (
        ("answerable", answerable),
        ("unanswerable", unanswerable),
    ):
        lines.append(
            f"| {label} | {int(values['count'])} | {values['min']:.4f} | "
            f"{values['p25']:.4f} | {values['median']:.4f} | "
            f"{values['p75']:.4f} | {values['max']:.4f} | "
            f"{values['mean']:.4f} |"
        )
    return lines


def _failure_reason(
    result: CaseResult,
    chunks_by_id: dict[str, RuleChunk],
) -> str:
    top = result.results[0]
    gold = chunks_by_id[result.case.gold_rule_ids[0]]
    if top.section == gold.section:
        return "同章节规则共享大量业务词，BM25 缺少语义消歧。"
    return "口语改写与 gold 规则字面重合较少，其他章节的高频词获得更高分。"


def build_report(
    *,
    evaluated: list[CaseResult],
    chunks: list[RuleChunk],
    internal_count: int,
    k1: float,
    b: float,
) -> str:
    answerable = [result for result in evaluated if result.case.answerable]
    unanswerable = [result for result in evaluated if not result.case.answerable]
    answerable_scores = [result.results[0].score for result in answerable]
    unanswerable_scores = [result.results[0].score for result in unanswerable]
    chunks_by_id = {chunk.rule_id: chunk for chunk in chunks}
    failures = [
        result
        for result in answerable
        if result.first_gold_rank != 1
    ]

    lines = [
        (
            "## D1.1 客服业务规则 baseline — "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        ),
        "",
        f"- 规则数：public={len(chunks)}，internal={internal_count}",
        f"- Eval：{len(evaluated)}（answerable={len(answerable)}，unanswerable={len(unanswerable)}）",
        f"- BM25 参数：k1={k1}，b={b}",
        "",
        "### Answerable 指标",
        "",
        *_metric_table(answerable),
        "",
        "### Top1 score 分布",
        "",
        *_distribution_table(answerable_scores, unanswerable_scores),
        "",
        "### Unanswerable 每条 Top1",
        "",
        "| case_id | query | top1 | score |",
        "|---|---|---|---:|",
    ]
    for result in unanswerable:
        top = result.results[0]
        lines.append(
            f"| {result.case.case_id} | {result.case.query} | "
            f"{top.rule_id} | {top.score:.4f} |"
        )

    lines.extend(
        [
            "",
            "### Recall@1 失败明细",
            "",
            "| query | gold rule | BM25 top results | 失败原因 |",
            "|---|---|---|---|",
        ]
    )
    for result in failures:
        gold = ", ".join(result.case.gold_rule_ids)
        top_results = "; ".join(
            f"{item.rule_id} ({item.score:.4f})"
            for item in result.results[:3]
        )
        lines.append(
            f"| {result.case.query} | {gold} | {top_results} | "
            f"{_failure_reason(result, chunks_by_id)} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="BM25 规则检索基线评测")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--k1", type=float, default=1.5)
    parser.add_argument("--b", type=float, default=0.75)
    arguments = parser.parse_args()

    all_rules = load_rule_chunks(arguments.rules, include_internal=True)
    chunks = [rule for rule in all_rules if rule.visibility == "public"]
    internal_count = sum(
        rule.visibility == "internal" for rule in all_rules
    )
    cases = load_cases(arguments.cases)
    known_rule_ids = {chunk.rule_id for chunk in chunks}
    unknown_gold = sorted(
        {
            rule_id
            for case in cases
            for rule_id in case.gold_rule_ids
            if rule_id not in known_rule_ids
        }
    )
    if unknown_gold:
        raise ValueError(f"Eval 引用了不存在的规则: {unknown_gold}")

    index = BM25Index(chunks, k1=arguments.k1, b=arguments.b)
    evaluated = evaluate(index, cases)
    report = build_report(
        evaluated=evaluated,
        chunks=chunks,
        internal_count=internal_count,
        k1=arguments.k1,
        b=arguments.b,
    )
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    existed = arguments.report.exists() and arguments.report.stat().st_size > 0
    with arguments.report.open("a", encoding="utf-8", newline="\n") as file:
        if not existed:
            file.write("# BM25 规则检索评测\n\n")
        file.write(report)

    print(report)


if __name__ == "__main__":
    main()
