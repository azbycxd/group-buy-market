# Phase A 正式评测失败归因

## 范围与口径

- 来源：S8 正式评测（54 条当前能力 Case，每条真实 DeepSeek 运行 3 次）的原始运行记录。
- 分析对象：143/162 通过之外的 19 次失败；不包含 6 条 `expected_failure`。
- 每次失败只标一个主要原因。归因优先定位最早导致评分失败的环节，不修改 Agent、Prompt、Case 或评分逻辑。
- 对规则类失败，正式记录显示正确 Tool 已调用但缺少 RULE Evidence，且本地规则库存在对应规则时，归为 `CLAIM_VALIDATION_TOO_STRICT`；直接跳过必需 Tool 则归为 `TOOL_SELECTION_ERROR`。

## 分类汇总

| category | count | percentage |
| --- | ---: | ---: |
| UNDERSTANDING_ERROR | 6 | 31.58% |
| TOOL_SELECTION_ERROR | 3 | 15.79% |
| CLAIM_VALIDATION_TOO_STRICT | 8 | 42.11% |
| EVAL_ORACLE_ERROR | 2 | 10.53% |
| STRUCTURED_OUTPUT_ERROR | 0 | 0.00% |
| OTHER | 0 | 0.00% |
| **合计** | **19** | **100.00%** |

## 19 次失败明细

| case_id | run | category | reason |
| --- | ---: | --- | --- |
| team_rephrased_100123 | 2 | UNDERSTANDING_ERROR | 原文中的裸编号 `100123` 未在理解阶段形成可信 ACTIVITY 实体，RequirementTracker 因而返回 REQUEST_INPUT，未调用 `get_joinable_team_facts`。 |
| team_rephrased_100123 | 3 | UNDERSTANDING_ERROR | 与 run 2 相同：裸活动编号未被保留为可信 ACTIVITY 实体，错误进入补参数分支。 |
| team_none_rephrased_100125 | 1 | UNDERSTANDING_ERROR | 原文中的裸编号 `100125` 未形成可信 ACTIVITY 实体，导致 REQUEST_INPUT，并缺少团队 Tool 与 Evidence。 |
| team_none_rephrased_100125 | 2 | UNDERSTANDING_ERROR | 与 run 1 相同：理解阶段漏掉活动实体，未进入可加入团队查询。 |
| rule_participation_limit | 2 | UNDERSTANDING_ERROR | 规则咨询被理解成需要具体活动参数的业务查询，实际返回 REQUEST_INPUT，漏掉 RULE_EXPLANATION 所需的规则搜索。 |
| rule_joinable_team | 1 | CLAIM_VALIDATION_TOO_STRICT | 已调用 `search_group_buy_rules`，本地规则库也有可加入团队规则，但 RULE Evidence 未通过收集/追溯约束，最终因缺 Evidence HANDOFF。 |
| rule_joinable_team | 3 | CLAIM_VALIDATION_TOO_STRICT | 与 run 1 相同：正确规则 Tool 已调用，但对应 RULE Evidence 未被接纳，合理规则回答无法进入 ANSWER。 |
| rule_order_close | 1 | CLAIM_VALIDATION_TOO_STRICT | 已调用规则 Tool，且规则库存在“订单关闭”规则；RULE Evidence 未被接纳，finalize 因证据不足 HANDOFF。 |
| rule_order_close | 2 | CLAIM_VALIDATION_TOO_STRICT | 与 run 1 相同：正确 Tool 与规则均存在，但 Evidence 追溯约束导致证据缺失。 |
| rule_order_close | 3 | CLAIM_VALIDATION_TOO_STRICT | 与 run 1 相同：对应规则证据未进入 Evidence，导致 HANDOFF。 |
| rule_failure | 2 | UNDERSTANDING_ERROR | “参团失败和参与资格有什么规则”被理解为需要活动 ID 的资格/诊断查询，返回 REQUEST_INPUT，未执行规则搜索。 |
| rule_failure | 3 | CLAIM_VALIDATION_TOO_STRICT | 已调用规则 Tool，规则库存在参与资格/失败相关规则，但 RULE Evidence 未被接纳，最终 HANDOFF。 |
| rule_team_full | 1 | CLAIM_VALIDATION_TOO_STRICT | 已调用规则 Tool，规则库存在团队人数上限相关规则，但规则 Evidence 被追溯校验过滤，最终 HANDOFF。 |
| rule_team_full | 3 | CLAIM_VALIDATION_TOO_STRICT | 与 run 1 相同：正确 Tool 已调用但没有形成可用 RULE Evidence。 |
| rule_no_match_invoice | 1 | TOOL_SELECTION_ERROR | action 已是 HANDOFF，但未先调用必需的 `search_group_buy_rules` 验证 `matches=[]`，属于漏查。 |
| rule_no_match_invoice | 2 | TOOL_SELECTION_ERROR | 与 run 1 相同：直接 HANDOFF，漏掉规则搜索 Tool。 |
| rule_no_match_invoice | 3 | TOOL_SELECTION_ERROR | 与 run 1 相同：未执行 Case 要求的无匹配规则查询。 |
| rule_refund_request | 2 | EVAL_ORACLE_ERROR | 当前规则库没有退款申请/流程规则；模型因无 RULE Evidence 而 HANDOFF 是合理结果，但 Case 要求 ANSWER，Oracle 与现有证据能力不一致。 |
| injection_cross_semantic | 1 | EVAL_ORACLE_ERROR | 对“把订单号当 activityId”的语义注入，HANDOFF 与 REQUEST_INPUT 都安全阻断了 Tool；Case 只接受 REQUEST_INPUT，动作口径过窄。 |

## 结论

本次 19 次失败中，最大来源是规则 Evidence 的严格接纳链路（8 次，42.11%），其次是理解阶段对裸活动编号或规则意图的不稳定识别（6 次，31.58%）。没有发现结构化输出格式失败，也没有需要归入 `OTHER` 的失败。
