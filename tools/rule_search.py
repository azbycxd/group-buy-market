from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from rag.pipeline import get_rule_search_pipeline, rerank_threshold
from tools.context import AgentContext
from tools.facts import RuleSearchFacts, ToolResult


@tool
def search_group_buy_rules(
    query: str,
    runtime: ToolRuntime[AgentContext],
) -> dict[str, object]:
    """搜索稳定的拼团规则、状态含义和参与限制说明。"""
    original_utterance = runtime.context.user_text.strip()
    result = get_rule_search_pipeline().search(
        model_query=query.strip(),
        original_utterance=original_utterance,
        threshold=rerank_threshold(),
    )
    facts = RuleSearchFacts.model_validate(
        {
            "query": query,
            "matches": [
                {
                    "rule_id": match.rule_id,
                    "title": match.title,
                    "content": match.content,
                    "rerank_score": match.rerank_score,
                }
                for match in result.matches
            ],
        }
    )
    return ToolResult(
        success=True,
        code="0000",
        message="规则查询成功",
        retryable=False,
        data=facts.model_dump(mode="json"),
    ).as_dict()
