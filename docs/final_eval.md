# E1 Final Evaluation — 69 × 3

## Run metadata

- Git commit: `4ba07180e0216b67892ccae27b32497a46d0509e`
- Branch: `main`
- Model: `deepseek-chat`
- Started: `2026-09-28T23:07:14+08:00`
- Completed: `2026-09-28T23:19:55+08:00`
- Total elapsed: `761.193s`
- Parameters: `EVAL_RUNS_PER_CASE=3`; sequential runs; independent thread IDs
- Working tree dirty at start: `true` (evaluation/report instrumentation only)

## Core metrics

- Single-run pass rate: **204/207 (98.55%)**
- Wilson 95% confidence interval: **[95.83%, 99.51%]**
- Case-level stable pass rate: **66/69 (95.65%)**

| Case result | Cases |
|---|---:|
| 3/3 PASS | 66 |
| 2/3 PASS | 3 |
| 1/3 PASS | 0 |
| 0/3 PASS | 0 |

## Category statistics

| Category | Cases | Runs | Pass | Single-run Pass Rate |
|---|---:|---:|---:|---:|
| 参团诊断 | 12 | 36 | 36 | 100.00% |
| 订单 | 10 | 30 | 30 | 100.00% |
| 可加入团队 | 8 | 24 | 23 | 95.83% |
| 规则问答 | 10 | 30 | 29 | 96.67% |
| 能力不支持 | 8 | 24 | 24 | 100.00% |
| 参数 / 注入 | 6 | 18 | 18 | 100.00% |
| 多轮补参数 | 6 | 18 | 17 | 94.44% |
| 多轮状态隔离 | 3 | 9 | 9 | 100.00% |
| 规则答案引用 | 6 | 18 | 18 | 100.00% |

## Stability summary

| Metric | Cases |
|---|---:|
| At least one failed run | 3 |
| Failed exactly 1/3 | 3 |
| Failed at least 2/3 | 0 |
| Failed 3/3 | 0 |

## Failure-stage aggregation

| Failure Stage | Failed Runs | Affected Cases |
|---|---:|---:|
| CLAIM_VALIDATION | 1 | 1 |
| ENTITY_RESOLUTION | 1 | 1 |
| UNDERSTANDING | 1 | 1 |

## Failed runs

| case_id | Run | Expected | Actual | Failure Stage | Reason |
|---|---:|---|---|---|---|
| `team_rephrased_100123` | 1 | ANSWER | REQUEST_INPUT | ENTITY_RESOLUTION | action 允许 ANSWER，实际 REQUEST_INPUT; 缺少 Tool: get_joinable_team_facts; 缺少 Evidence: get_joinable_team_facts.candidateTeams |
| `rule_activity_validity` | 1 | ANSWER | REQUEST_INPUT | UNDERSTANDING | action 允许 ANSWER，实际 REQUEST_INPUT; 缺少 Tool: search_group_buy_rules; 缺少 Evidence: search_group_buy_rules.matches.* |
| `multi_diagnosis` | 1 | ANSWER | HANDOFF | CLAIM_VALIDATION | action 允许 ANSWER，实际 HANDOFF |

## Failed-case summary

| case_id | Failures / 3 | Stage | Summary |
|---|---:|---|---|
| `team_rephrased_100123` | 1/3 | ENTITY_RESOLUTION | action 允许 ANSWER，实际 REQUEST_INPUT; 缺少 Tool: get_joinable_team_facts; 缺少 Evidence: get_joinable_team_facts.candidateTeams |
| `rule_activity_validity` | 1/3 | UNDERSTANDING | action 允许 ANSWER，实际 REQUEST_INPUT; 缺少 Tool: search_group_buy_rules; 缺少 Evidence: search_group_buy_rules.matches.* |
| `multi_diagnosis` | 1/3 | CLAIM_VALIDATION | action 允许 ANSWER，实际 HANDOFF |

## Token usage

- Input tokens: **614051**
- Output tokens: **99751**
- Total tokens: **713802**

Machine-readable results: `evals/results/final_69x3.json`
