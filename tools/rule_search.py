from langchain.tools import tool

from tools.facts import RuleSearchFacts, ToolResult


RULES = (
    {
        "rule_id": "order_close",
        "title": "订单关闭",
        "content": "订单状态为 CLOSE 表示该拼团订单已关闭，不能继续参与当前拼团。",
        "keywords": ("订单", "关闭", "close"),
    },
    {
        "rule_id": "activity_validity",
        "title": "活动有效期",
        "content": "只有活动状态和有效时间窗口均允许时，用户才能继续参与拼团。",
        "keywords": ("活动", "过期", "有效期", "状态"),
    },
    {
        "rule_id": "participation_eligibility",
        "title": "参与资格与次数",
        "content": "参与资格受活动人群门禁和参与次数上限共同约束。",
        "keywords": ("资格", "次数", "参与", "参团", "失败"),
    },
    {
        "rule_id": "joinable_team",
        "title": "可加入团队",
        "content": "可加入团队必须仍在有效期内，且未达到目标人数或锁定人数上限。",
        "keywords": ("团队", "拼团", "加入", "队伍"),
    },
)


@tool
def search_group_buy_rules(query: str) -> dict[str, object]:
    """搜索稳定的拼团规则、状态含义和参与限制说明。"""
    normalized = query.strip().lower()
    matches = [
        {key: value for key, value in rule.items() if key != "keywords"}
        for rule in RULES
        if any(keyword in normalized for keyword in rule["keywords"])
    ]
    if not matches:
        matches = [
            {key: value for key, value in rule.items() if key != "keywords"}
            for rule in RULES[:3]
        ]

    facts = RuleSearchFacts.model_validate(
        {"query": query, "matches": matches}
    )
    return ToolResult(
        success=True,
        code="0000",
        message="规则查询成功",
        retryable=False,
        data=facts.model_dump(mode="json"),
    ).as_dict()
