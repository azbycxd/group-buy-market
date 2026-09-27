from langchain.tools import ToolRuntime, tool

from tools.context import AgentContext
from tools.facts import RuleSearchFacts, ToolResult


RULES = (
    {
        "rule_id": "order_close",
        "title": "订单关闭",
        "content": "订单状态为 CLOSE 表示该拼团订单已关闭，不能继续参与当前拼团。",
        "keywords": ("订单关闭", "关闭", "close", "订单状态"),
    },
    {
        "rule_id": "activity_validity",
        "title": "活动有效期",
        "content": "只有活动状态和有效时间窗口均允许时，用户才能继续参与拼团。",
        "keywords": (
            "活动有效期",
            "有效期",
            "有效时间",
            "时间窗口",
            "过期",
            "失效",
        ),
    },
    {
        "rule_id": "participation_eligibility",
        "title": "参与资格与次数",
        "content": "参与资格受活动人群门禁和参与次数上限共同约束。",
        "keywords": (
            "参与资格",
            "资格",
            "参与次数",
            "次数",
            "次数上限",
            "参团失败",
            "参加不了",
            "不能参加",
            "人群门禁",
        ),
    },
    {
        "rule_id": "joinable_team",
        "title": "可加入团队",
        "content": "可加入团队必须仍在有效期内，且未达到目标人数或锁定人数上限。",
        "keywords": (
            "可加入团队",
            "加入团队",
            "团队",
            "队伍",
            "人数上限",
            "目标人数",
            "锁定人数",
            "满员",
        ),
    },
)


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(keyword.lower() in text for keyword in keywords)


@tool
def search_group_buy_rules(
    query: str,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """搜索稳定的拼团规则、状态含义和参与限制说明。"""
    normalized_query = query.strip().lower()
    normalized_user_text = runtime.context.user_text.strip().lower()
    ranked: list[tuple[int, int, dict[str, object]]] = []
    for position, rule in enumerate(RULES):
        keywords = rule["keywords"]
        user_hits = _keyword_hits(normalized_user_text, keywords)
        if user_hits == 0:
            continue
        query_hits = _keyword_hits(normalized_query, keywords)
        ranked.append((
            user_hits * 10 + query_hits,
            -position,
            {key: value for key, value in rule.items() if key != "keywords"},
        ))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    matches = [item[2] for item in ranked]

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
