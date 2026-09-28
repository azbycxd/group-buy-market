from __future__ import annotations

import unittest

import yaml

from rag.bm25 import BM25Index, tokenize
from rag.chunking import RuleChunk, load_rule_chunks
from scripts.rag_eval import DEFAULT_CASES_PATH


class RuleRetrievalBaselineTests(unittest.TestCase):
    def test_only_public_rules_are_indexed(self) -> None:
        chunks = load_rule_chunks()
        all_rules = load_rule_chunks(include_internal=True)
        self.assertEqual(len(all_rules), 48)
        self.assertEqual(len(chunks), 40)
        self.assertEqual(
            sum(rule.visibility == "internal" for rule in all_rules),
            8,
        )
        self.assertEqual(len({chunk.rule_id for chunk in all_rules}), 48)
        self.assertTrue(all(chunk.visibility == "public" for chunk in chunks))
        self.assertTrue(
            all(
                chunk.section and chunk.title and chunk.content
                for chunk in chunks
            )
        )
        forbidden = (
            "SQL",
            "Mapper",
            "lock_count",
            "E0103",
            "UPDATE_ZERO",
            "@Transactional",
            "Redis",
            "BitSet",
            "DCC",
            "hash",
        )
        self.assertTrue(
            all(
                not any(term in rule.text for term in forbidden)
                for rule in all_rules
            )
        )

    def test_bm25_prefers_exact_business_concept(self) -> None:
        chunks = [
            RuleChunk("A-001", "活动", "时间", "活动什么时候结束"),
            RuleChunk("B-001", "退款", "到账", "不能确认退款到账"),
        ]
        result = BM25Index(chunks).search("退款到账了吗", top_k=2)
        self.assertEqual(result[0].rule_id, "B-001")
        self.assertGreater(result[0].score, result[1].score)

    def test_business_dictionary_keeps_core_terms(self) -> None:
        tokens = tokenize("先做退款预检，再看可加入团队")
        self.assertIn("退款预检", tokens)
        self.assertIn("可加入团队", tokens)

    def test_source_metadata_does_not_participate_in_search(self) -> None:
        chunk = RuleChunk(
            "A-001",
            "业务",
            "普通标题",
            "普通内容",
            source=("OnlyInJavaSourceToken",),
        )
        result = BM25Index([chunk]).search("OnlyInJavaSourceToken")
        self.assertEqual(result[0].score, 0.0)
        self.assertNotIn("OnlyInJavaSourceToken", chunk.text)

    def test_eval_has_expected_answerable_split(self) -> None:
        cases = yaml.safe_load(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 40)
        self.assertEqual(
            sum(bool(case["gold_rule_ids"]) for case in cases),
            30,
        )
        self.assertEqual(
            sum(not case["gold_rule_ids"] for case in cases),
            10,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
