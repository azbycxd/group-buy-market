from __future__ import annotations

import argparse
import os
import platform
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import numpy as np
import yaml

from rag.bm25 import BM25Index, SearchResult
from rag.chunking import DEFAULT_RULES_PATH, RuleChunk, load_rule_chunks
from rag.dense import DEFAULT_CACHE_DIR, MODEL_NAME as DENSE_MODEL_NAME, DenseIndex
from rag.fusion import RRFIndex
from rag.rerank import (
    CANDIDATE_COUNT as RERANK_CANDIDATE_COUNT,
    MODEL_NAME as RERANK_MODEL_NAME,
    RerankIndex,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = PROJECT_ROOT / "evals" / "rule_retrieval.yaml"
DEFAULT_HARD_CASES_PATH = (
    PROJECT_ROOT / "evals" / "rule_retrieval_hard.yaml"
)
DEFAULT_REPORT_PATH = PROJECT_ROOT / "docs" / "rag_eval.md"
METHODS = ("BM25", "Dense", "RRF", "RRF+Rerank")
FOCUS_CASE_IDS = (
    "activity_not_active",
    "joinable_team_conditions",
    "formed_refund_review",
)


class SearchIndex(Protocol):
    chunks: list[RuleChunk]

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]: ...


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


@dataclass(frozen=True, slots=True)
class ThresholdResult:
    threshold: float
    false_reject: int
    false_answer: int
    true_positive_rate: float
    true_negative_rate: float
    balanced_accuracy: float


def load_cases(path: str | Path) -> list[RetrievalCase]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("cases")
    if not isinstance(raw, list):
        raise ValueError("检索评测 YAML 顶层必须是列表或包含 cases 列表")
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
    index: SearchIndex,
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


def metrics(results: list[CaseResult]) -> dict[str, float]:
    if not results:
        raise ValueError("指标至少需要一条 Case")
    return {
        f"Recall@{at}": sum(
            result.first_gold_rank is not None
            and result.first_gold_rank <= at
            for result in results
        )
        / len(results)
        for at in (1, 3, 5)
    } | {
        "MRR": statistics.fmean(
            0.0
            if result.first_gold_rank is None
            else 1 / result.first_gold_rank
            for result in results
        )
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("百分位至少需要一个值")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def score_distribution(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("分数分布至少需要一个值")
    return {
        "count": float(len(values)),
        "min": min(values),
        "p25": _percentile(values, 0.25),
        "median": statistics.median(values),
        "p75": _percentile(values, 0.75),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def select_threshold(
    answerable_scores: list[float],
    unanswerable_scores: list[float],
) -> ThresholdResult:
    if not answerable_scores or not unanswerable_scores:
        raise ValueError("阈值探索需要 answerable 和 unanswerable 样本")
    all_scores = answerable_scores + unanswerable_scores
    candidates = sorted(set(all_scores))
    candidates.append(float(np.nextafter(max(all_scores), np.inf)))
    selections: list[ThresholdResult] = []
    for threshold in candidates:
        false_reject = sum(score < threshold for score in answerable_scores)
        false_answer = sum(score >= threshold for score in unanswerable_scores)
        tpr = 1 - false_reject / len(answerable_scores)
        tnr = 1 - false_answer / len(unanswerable_scores)
        selections.append(
            ThresholdResult(
                threshold=threshold,
                false_reject=false_reject,
                false_answer=false_answer,
                true_positive_rate=tpr,
                true_negative_rate=tnr,
                balanced_accuracy=(tpr + tnr) / 2,
            )
        )
    return max(
        selections,
        key=lambda item: (
            item.balanced_accuracy,
            -item.false_answer,
            -item.false_reject,
            item.threshold,
        ),
    )


def _method_table(
    evaluations: dict[str, list[CaseResult]],
) -> list[str]:
    lines = [
        "| Method | Recall@1 | Recall@3 | Recall@5 | MRR |",
        "|---|---:|---:|---:|---:|",
    ]
    for method in METHODS:
        values = metrics(evaluations[method])
        lines.append(
            f"| {method} | {values['Recall@1']:.4f} | "
            f"{values['Recall@3']:.4f} | {values['Recall@5']:.4f} | "
            f"{values['MRR']:.4f} |"
        )
    return lines


def _distribution_table(
    signals: dict[str, tuple[list[float], list[float]]],
) -> list[str]:
    lines = [
        "| Signal | 类型 | count | min | p25 | median | p75 | max | mean |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for signal, (answerable, unanswerable) in signals.items():
        for label, scores in (
            ("answerable", answerable),
            ("unanswerable", unanswerable),
        ):
            values = score_distribution(scores)
            lines.append(
                f"| {signal} | {label} | {int(values['count'])} | "
                f"{values['min']:.4f} | {values['p25']:.4f} | "
                f"{values['median']:.4f} | {values['p75']:.4f} | "
                f"{values['max']:.4f} | {values['mean']:.4f} |"
            )
    return lines


def _threshold_table(
    thresholds: dict[str, ThresholdResult],
) -> list[str]:
    lines = [
        "| Signal | Threshold | False Reject | False Answer | TPR | TNR | Balanced Accuracy |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for signal, result in thresholds.items():
        lines.append(
            f"| {signal} | {result.threshold:.6f} | "
            f"{result.false_reject} | {result.false_answer} | "
            f"{result.true_positive_rate:.4f} | "
            f"{result.true_negative_rate:.4f} | "
            f"{result.balanced_accuracy:.4f} |"
        )
    return lines


def _by_case(results: list[CaseResult]) -> dict[str, CaseResult]:
    return {result.case.case_id: result for result in results}


def _top(result: CaseResult) -> str:
    if not result.results:
        return "-"
    item = result.results[0]
    return f"{item.rule_id} ({item.score:.4f})"


def _top_n(result: CaseResult, count: int = 3) -> str:
    return "; ".join(
        f"{item.rule_id} ({item.score:.4f})"
        for item in result.results[:count]
    )


def _focus_table(
    rrf: dict[str, CaseResult],
    rerank: dict[str, CaseResult],
) -> list[str]:
    lines = [
        "| case_id | query | gold | RRF Top3 | Rerank Top3 | RRF gold rank | Rerank gold rank | 救回 |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    for case_id in FOCUS_CASE_IDS:
        before = rrf[case_id]
        after = rerank[case_id]
        rescued = before.first_gold_rank != 1 and after.first_gold_rank == 1
        lines.append(
            f"| {case_id} | {before.case.query} | "
            f"{', '.join(before.case.gold_rule_ids)} | {_top_n(before)} | "
            f"{_top_n(after)} | {before.first_gold_rank or '未召回'} | "
            f"{after.first_gold_rank or '未进 Top10'} | "
            f"{'是' if rescued else '否'} |"
        )
    return lines


def _change_table(
    case_ids: list[str],
    rrf: dict[str, CaseResult],
    rerank: dict[str, CaseResult],
) -> list[str]:
    lines = [
        "| case_id | query | gold | RRF top1 | Rerank top1 |",
        "|---|---|---|---|---|",
    ]
    if not case_ids:
        lines.append("| — | 无 | — | — | — |")
        return lines
    for case_id in case_ids:
        before = rrf[case_id]
        lines.append(
            f"| {case_id} | {before.case.query} | "
            f"{', '.join(before.case.gold_rule_ids)} | {_top(before)} | "
            f"{_top(rerank[case_id])} |"
        )
    return lines


def _machine_environment() -> str:
    import torch

    cpu = platform.processor() or os.getenv("PROCESSOR_IDENTIFIER", "unknown")
    return (
        f"{platform.platform()}；CPU={cpu}；logical_cores={os.cpu_count()}；"
        f"PyTorch={torch.__version__}；torch_threads={torch.get_num_threads()}"
    )


def build_d21_report(
    *,
    formal: dict[str, list[CaseResult]],
    hard: dict[str, list[CaseResult]],
    formal_all: dict[str, list[CaseResult]],
    signals: dict[str, tuple[list[float], list[float]]],
    thresholds: dict[str, ThresholdResult],
    latencies_ms: list[float],
) -> str:
    formal_rrf = _by_case(formal["RRF"])
    formal_rerank = _by_case(formal["RRF+Rerank"])
    rrf = _by_case(formal["RRF"] + hard["RRF"])
    rerank = _by_case(
        formal["RRF+Rerank"] + hard["RRF+Rerank"]
    )
    rescued = [
        case_id
        for case_id, result in rrf.items()
        if result.first_gold_rank != 1
        and rerank[case_id].first_gold_rank == 1
    ]
    regressed = [
        case_id
        for case_id, result in rrf.items()
        if result.first_gold_rank == 1
        and rerank[case_id].first_gold_rank != 1
    ]
    latency_mean = statistics.fmean(latencies_ms)
    latency_p50 = _percentile(latencies_ms, 0.50)
    latency_p95 = _percentile(latencies_ms, 0.95)
    formal_answerable = sum(
        result.case.answerable for result in formal_all["Dense"]
    )
    formal_unanswerable = len(formal_all["Dense"]) - formal_answerable

    lines = [
        (
            "## D2.1 CrossEncoder Rerank + 拒答信号对比 — "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        ),
        "",
        f"- Dense model：`{DENSE_MODEL_NAME}`",
        f"- Reranker：`{RERANK_MODEL_NAME}`，sentence-transformers CrossEncoder，CPU",
        f"- 候选：RRF Top{RERANK_CANDIDATE_COUNT}；输入仅为 `(query, title + content)`",
        f"- 样本：正式集 answerable={formal_answerable}、unanswerable={formal_unanswerable}；hard answerable={len(hard['Dense'])}",
        "",
        "### 正式集（30 answerable）",
        "",
        *_method_table(formal),
        "",
        "### 人工 hard 集（10 answerable，独立统计）",
        "",
        *_method_table(hard),
        "",
        "### Rerank CPU 延迟",
        "",
        f"- 环境：{_machine_environment()}",
        f"- 候选数：Top{RERANK_CANDIDATE_COUNT}；query 数：{len(latencies_ms)}",
        f"- mean={latency_mean:.2f} ms，p50={latency_p50:.2f} ms，p95={latency_p95:.2f} ms",
        "- 计时范围：构造 CrossEncoder 输入对、CPU 推理、分数排序；不包含 RRF 召回。",
        "",
        "### 三个重点相邻规则失败",
        "",
        *_focus_table(formal_rrf, formal_rerank),
        "",
        "### Rerank 相对 RRF 救回（正式集 + hard 集）",
        "",
        *_change_table(rescued, rrf, rerank),
        "",
        "### Rerank 相对 RRF 新变差（正式集 + hard 集）",
        "",
        *_change_table(regressed, rrf, rerank),
        "",
        "### 拒答信号分布（answerable=正式30+hard10，unanswerable=正式10）",
        "",
        *_distribution_table(signals),
        "",
        "### 探索性拒答阈值",
        "",
        *_threshold_table(thresholds),
        "",
        "> 阈值和指标来自同一小样本集，仅作为 D3 的探索信号，不能视为泛化性能结论。",
        "",
    ]
    return "\n".join(lines)


def _validate_gold(
    cases: list[RetrievalCase], known_rule_ids: set[str]
) -> None:
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


def _top1_scores(results: list[CaseResult]) -> list[float]:
    return [result.results[0].score for result in results]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BM25、Dense、RRF 与 CrossEncoder Rerank 评测"
    )
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--hard-cases", type=Path, default=DEFAULT_HARD_CASES_PATH
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--k1", type=float, default=1.5)
    parser.add_argument("--b", type=float, default=0.75)
    parser.add_argument("--dense-cache", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--rrf-k", type=int, default=60)
    arguments = parser.parse_args()

    chunks = load_rule_chunks(arguments.rules)
    formal_cases = load_cases(arguments.cases)
    hard_cases = load_cases(arguments.hard_cases)
    known_rule_ids = {chunk.rule_id for chunk in chunks}
    _validate_gold(formal_cases + hard_cases, known_rule_ids)

    bm25_index = BM25Index(chunks, k1=arguments.k1, b=arguments.b)
    dense_index = DenseIndex(
        chunks,
        model_name=DENSE_MODEL_NAME,
        cache_dir=arguments.dense_cache,
    )
    rrf_index = RRFIndex(
        bm25_index, dense_index, k=arguments.rrf_k, candidate_count=20
    )
    rerank_index = RerankIndex(
        rrf_index,
        model_name=RERANK_MODEL_NAME,
        candidate_count=RERANK_CANDIDATE_COUNT,
        batch_size=RERANK_CANDIDATE_COUNT,
        device="cpu",
    )
    indexes: dict[str, SearchIndex] = {
        "BM25": bm25_index,
        "Dense": dense_index,
        "RRF": rrf_index,
        "RRF+Rerank": rerank_index,
    }
    formal_all = {
        method: evaluate(index, formal_cases)
        for method, index in indexes.items()
    }
    hard_all = {
        method: evaluate(index, hard_cases)
        for method, index in indexes.items()
    }
    formal = {
        method: [result for result in results if result.case.answerable]
        for method, results in formal_all.items()
    }

    dense_answerable = _top1_scores(formal["Dense"] + hard_all["Dense"])
    dense_unanswerable = _top1_scores(
        [
            result
            for result in formal_all["Dense"]
            if not result.case.answerable
        ]
    )
    rerank_answerable = _top1_scores(
        formal["RRF+Rerank"] + hard_all["RRF+Rerank"]
    )
    rerank_unanswerable = _top1_scores(
        [
            result
            for result in formal_all["RRF+Rerank"]
            if not result.case.answerable
        ]
    )
    signals = {
        "Dense cosine": (dense_answerable, dense_unanswerable),
        "Rerank score": (rerank_answerable, rerank_unanswerable),
    }
    thresholds = {
        signal: select_threshold(answerable, unanswerable)
        for signal, (answerable, unanswerable) in signals.items()
    }
    report = build_d21_report(
        formal=formal,
        hard=hard_all,
        formal_all=formal_all,
        signals=signals,
        thresholds=thresholds,
        latencies_ms=rerank_index.latencies_ms,
    )

    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    existed = arguments.report.exists() and arguments.report.stat().st_size > 0
    with arguments.report.open("a", encoding="utf-8", newline="\n") as file:
        if not existed:
            file.write("# 规则检索评测\n\n")
        elif arguments.report.read_text(encoding="utf-8").endswith("\n"):
            file.write("\n")
        else:
            file.write("\n\n")
        file.write(report)

    print(report)


if __name__ == "__main__":
    main()
