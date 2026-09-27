from __future__ import annotations

import unittest

from understanding import (
    EntityMention,
    EntityType,
    resolve_entities,
    resolve_pending_entities,
)


class UnderstandingEntityTests(unittest.TestCase):
    def test_real_numeric_order_requires_an_order_label(self) -> None:
        parsed = resolve_entities(
            "订单 644398015396 现在什么状态",
            [
                EntityMention(
                    entity_type=EntityType.ORDER,
                    source_text="644398015396",
                )
            ],
        )
        self.assertEqual(parsed[0].value, "644398015396")

    def test_activity_number_cannot_be_retyped_as_an_order(self) -> None:
        parsed = resolve_entities(
            "活动 100123 还有效吗",
            [
                EntityMention(
                    entity_type=EntityType.ORDER,
                    source_text="100123",
                )
            ],
        )
        self.assertEqual(parsed, [])

    def test_pending_order_accepts_a_bare_numeric_value(self) -> None:
        parsed = resolve_pending_entities(
            "644398015396",
            [EntityType.ORDER],
        )
        self.assertEqual(parsed[0].value, "644398015396")

    def test_pending_order_does_not_consume_an_activity_topic_switch(self) -> None:
        parsed = resolve_pending_entities(
            "算了，查询活动 100123 是否有效",
            [EntityType.ORDER],
        )
        self.assertEqual(parsed, [])

    def test_existing_prefixed_order_still_parses(self) -> None:
        parsed = resolve_entities(
            "订单 ORD100001 现在什么状态",
            [
                EntityMention(
                    entity_type=EntityType.ORDER,
                    source_text="ORD100001",
                )
            ],
        )
        self.assertEqual(parsed[0].value, "ORD100001")


if __name__ == "__main__":
    unittest.main()
