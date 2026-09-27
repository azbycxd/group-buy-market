from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import _create_model
from understanding import create_understander, understand_text


QUESTIONS = (
    "订单 ORD100001 现在什么状态？",
    "帮我看看 ORD100002 还能不能继续参团。",
    "活动100123现在还有效吗？",
    "100124 这个活动是不是过期了？",
    "我有资格参加活动100123吗？",
    "活动 100124 我的参与次数用完了吗？",
    "活动100123还有能加入的团吗？",
    "帮我找一下活动 100125 的可加入团队。",
    "拼团资格和参与次数有什么规则？",
    "优惠能否与会员券叠加？",
    "为什么我不能参加活动100123",
    "为啥我参加不了活动100123",
    "请综合看看活动 100125 为什么参团失败。",
    "订单 ORD100001 CLOSE了，钱退回来没有",
    "CLOSE了，钱退回来没有",
    "订单 ORD100001 怎么申请退款？",
    "拼团失败后满足什么条件可以退款？",
    "帮我把订单状态改掉",
    "请直接把订单 ORD100001 的状态修改成 NORMAL。",
    "帮我写一首关于春天的诗。",
)


def main() -> None:
    understander = create_understander(_create_model())
    for index, question in enumerate(QUESTIONS, start=1):
        understanding, parsed_entities = understand_text(understander, question)
        needs = [need.value for need in understanding.needs]
        entities = [entity.as_dict() for entity in parsed_entities]
        print(f"{index:02d}. 用户原文: {question}")
        print(f"    needs: {json.dumps(needs, ensure_ascii=False)}")
        print(f"    entities: {json.dumps(entities, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
