from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import yaml

from rag.bm25 import BM25Index, SearchResult
from rag.chunking import DEFAULT_RULES_PATH, RuleChunk, load_rule_chunks
from rag.dense import DEFAULT_CACHE_DIR, MODEL_NAME, DenseIndex
from rag.fusion import RRFIndex


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = PROJECT_ROOT / "evals" / "rule_retrieval.yaml"
DEFAULT_HARD_CASES_PATH = (
    PROJECT_ROOT / "evals" / "rule_retrieval_hard.yaml"
)
DEFAULT_REPORT_PATH = PROJECT_ROOT / "docs" / "rag_eval.md"
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
        "Recall@1": sum(
            result.first_gold_rank is not None
            and result.first_gold_rank <= 1
            for result in results
        )
        / len(results),
        "Recall@3": sum(
            result.first_gold_rank is not None
            and result.first_gold_rank <= 3
            for result in results
        )
        / len(results),
        "Recall@5": sum(
            result.first_gold_rank is not None
            and result.first_gold_rank <= 5
            for result in results
        )
        / len(results),
        "MRR": statistics.fmean(
            0.0
            if result.first_gold_rank is None
            else 1 / result.first_gold_rank
            for result in results
        ),
    }


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


def _method_table(
    evaluations: dict[str, list[CaseResult]],
) -> list[str]:
    lines = [
        "| Method | Recall@1 | Recall@3 | Recall@5 | MRR |",
        "|---|---:|---:|---:|---:|",
    ]
    for method in ("BM25", "Dense", "RRF"):
        values = metrics(evaluations[method])
        lines.append(
            f"| {method} | {values['Recall@1']:.4f} | "
            f"{values['Recall@3']:.4f} | {values['Recall@5']:.4f} | "
            f"{values['MRR']:.4f} |"
        )
    return lines


def _distribution_table(
    answerable_scores: list[float],
    unanswerable_scores: list[float],
) -> list[str]:
    lines = [
        "| 类型 | count | min | p25 | median | p75 | max | mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, scores in (
        ("answerable", answerable_scores),
        ("unanswerable", unanswerable_scores),
    ):
        values = score_distribution(scores)
        lines.append(
            f"| {label} | {int(values['count'])} | {values['min']:.4f} | "
            f"{values['p25']:.4f} | {values['median']:.4f} | "
            f"{values['p75']:.4f} | {values['max']:.4f} | "
            f"{values['mean']:.4f} |"
        )
    return lines


def _by_case(results: list[CaseResult]) -> dict[str, CaseResult]:
    return {result.case.case_id: result for result in results}


def _top(result: CaseResult) -> str:
    if not result.results:
        return "-"
    item = result.results[0]
    return f"{item.rule_id} ({item.score:.4f})"


def _comparison_table(
    case_ids: list[str],
    bm25: dict[str, CaseResult],
    dense: dict[str, CaseResult],
    rrf: dict[str, CaseResult],
) -> list[str]:
    lines = [
        "| case_id | query | gold | BM25 top1 | Dense top1 | RRF top1 |",
        "|---|---|---|---|---|---|",
    ]
    if not case_ids:
        lines.append("| — | 无 | — | — | — | — |")
        return lines
    for case_id in case_ids:
        bm25_result = bm25[case_id]
        lines.append(
            f"| {case_id} | {bm25_result.case.query} | "
            f"{', '.join(bm25_result.case.gold_rule_ids)} | "
            f"{_top(bm25_result)} | {_top(dense[case_id])} | "
            f"{_top(rrf[case_id])} |"
        )
    return lines


def build_d2_report(
    *,
    formal: dict[str, list[CaseResult]],
    hard: dict[str, list[CaseResult]],
    dense_all_formal: list[CaseResult],
    model_name: str,
    rrf_k: int,
) -> str:
    bm25 = _by_case(formal["BM25"])
    dense = _by_case(formal["Dense"])
    rrf = _by_case(formal["RRF"])
    rescued = [
        case_id
        for case_id, result in bm25.items()
        if result.first_gold_rank != 1
        and rrf[case_id].first_gold_rank == 1
    ]
    regressed = [
        case_id
        for case_id, result in bm25.items()
        if result.first_gold_rank == 1
        and rrf[case_id].first_gold_rank != 1
    ]
    dense_rescued = [
        case_id
        for case_id, result in bm25.items()
        if result.first_gold_rank != 1
        and dense[case_id].first_gold_rank == 1
    ]
    dense_answerable_scores = [
        result.results[0].score
        for result in dense_all_formal
        if result.case.answerable
    ]
    dense_unanswerable_scores = [
        result.results[0].score
        for result in dense_all_formal
        if not result.case.answerable
    ]

    lines = [
        (
            "## D2 Dense Retrieval + RRF — "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        ),
        "",
        f"- Dense model：`{model_name}`",
        "- 文档向量文本：`title + content`；查询使用 BGE 检索前缀",
        f"- RRF：k={rrf_k}，BM25 top20 + Dense top20",
        "- 未设置 Dense 拒答阈值",
        "",
        "### 正式集（30 answerable）",
        "",
        *_method_table(formal),
        "",
        "### 人工 hard 集（10 answerable，独立统计）",
        "",
        *_method_table(hard),
        "",
        "### Dense Top1 cosine score 分布（正式集）",
        "",
        *_distribution_table(
            dense_answerable_scores, dense_unanswerable_scores
        ),
        "",
        "### A. BM25 Recall@1 失败、RRF Recall@1 成功",
        "",
        *_comparison_table(rescued, bm25, dense, rrf),
        "",
        "### B. BM25 Recall@1 成功、RRF Recall@1 失败",
        "",
        *_comparison_table(regressed, bm25, dense, rrf),
        "",
        "### C. Dense Recall@1 独有救回（相对 BM25）",
        "",
        *_comparison_table(dense_rescued, bm25, dense, rrf),
        "",
        "### D1.1 三条重点失败复查",
        "",
        *_comparison_table(
            list(FOCUS_CASE_IDS), bm25, dense, rrf
        ),
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


def main() -> None:
    parser = argparse.ArgumentParser(description="BM25、Dense 与 RRF 规则检索评测")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument(
        "--hard-cases", type=Path, default=DEFAULT_HARD_CASES_PATH
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--k1", type=float, default=1.5)
    parser.add_argument("--b", type=float, default=0.75)
    parser.add_argument("--dense-model", default=MODEL_NAME)
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
        model_name=arguments.dense_model,
        cache_dir=arguments.dense_cache,
    )
    rrf_index = RRFIndex(
        bm25_index, dense_index, k=arguments.rrf_k, candidate_count=20
    )

    formal_all = {
        "BM25": evaluate(bm25_index, formal_cases),
        "Dense": evaluate(dense_index, formal_cases),
        "RRF": evaluate(rrf_index, formal_cases),
    }
    formal = {
        method: [result for result in results if result.case.answerable]
        for method, results in formal_all.items()
    }
    hard = {
        "BM25": evaluate(bm25_index, hard_cases),
        "Dense": evaluate(dense_index, hard_cases),
        "RRF": evaluate(rrf_index, hard_cases),
    }
    report = build_d2_report(
        formal=formal,
        hard=hard,
        dense_all_formal=formal_all["Dense"],
        model_name=arguments.dense_model,
        rrf_k=arguments.rrf_k,
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
