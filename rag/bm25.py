from __future__ import annotations

import math
import re
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Sequence

import jieba

from rag.chunking import RuleChunk


BUSINESS_WORDS = (
    "拼团",
    "开团",
    "参团",
    "成团",
    "锁单",
    "活动有效期",
    "参与次数",
    "参与上限",
    "人群标签",
    "流量切分",
    "可加入团队",
    "团队库存",
    "目标人数",
    "订单状态",
    "支付结算",
    "超时未支付",
    "退款预检",
    "退款提议",
    "人工审核",
    "退款类型",
    "幂等键",
    "外部交易号",
    "退款到账",
    "通知任务",
)
TOKEN = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9_]+")

jieba.setLogLevel(logging.ERROR)
for word in BUSINESS_WORDS:
    jieba.add_word(word)


def tokenize(text: str) -> list[str]:
    normalized = text.lower().replace("|", " ")
    return [
        token
        for token in (piece.strip() for piece in jieba.lcut(normalized))
        if token and TOKEN.fullmatch(token)
    ]


@dataclass(frozen=True, slots=True)
class SearchResult:
    rule_id: str
    section: str
    title: str
    content: str
    score: float


class BM25Index:
    def __init__(
        self,
        chunks: Sequence[RuleChunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if not chunks:
            raise ValueError("BM25 至少需要一个规则 chunk")
        if k1 <= 0:
            raise ValueError("k1 必须大于 0")
        if not 0 <= b <= 1:
            raise ValueError("b 必须在 [0, 1] 范围内")
        self.chunks = list(chunks)
        self.k1 = k1
        self.b = b
        self._term_frequencies = [
            Counter(tokenize(chunk.text)) for chunk in self.chunks
        ]
        self._document_lengths = [
            sum(frequencies.values()) for frequencies in self._term_frequencies
        ]
        self._average_document_length = (
            sum(self._document_lengths) / len(self._document_lengths)
        )
        document_frequency: Counter[str] = Counter()
        for frequencies in self._term_frequencies:
            document_frequency.update(frequencies.keys())
        count = len(self.chunks)
        self._idf = {
            term: math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        query_terms = Counter(tokenize(query))
        scored: list[tuple[float, RuleChunk]] = []
        for chunk, frequencies, length in zip(
            self.chunks,
            self._term_frequencies,
            self._document_lengths,
            strict=True,
        ):
            score = 0.0
            length_normalization = 1 - self.b + self.b * (
                length / self._average_document_length
            )
            for term, query_frequency in query_terms.items():
                frequency = frequencies.get(term, 0)
                if frequency == 0:
                    continue
                numerator = frequency * (self.k1 + 1)
                denominator = frequency + self.k1 * length_normalization
                score += self._idf.get(term, 0.0) * (
                    numerator / denominator
                ) * query_frequency
            scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].rule_id))
        return [
            SearchResult(
                rule_id=chunk.rule_id,
                section=chunk.section,
                title=chunk.title,
                content=chunk.content,
                score=score,
            )
            for score, chunk in scored[: min(top_k, len(scored))]
        ]


def build_index(
    chunks: Iterable[RuleChunk],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> BM25Index:
    return BM25Index(list(chunks), k1=k1, b=b)
