# C 阶段退款故障注入验收

退款 action 的写请求只能由固定确认工作流发送。每个 action 最多主动发送一次
`POST /api/v1/agent/order/refund`；不确定结果及进程恢复只允许通过
`POST /api/v1/agent/order/refund/result` 对账。

| 场景 | 注入方式 | 期望结果 | 实际结果 | 对应测试 |
|---|---|---|---|---|
| Java 已执行，但响应丢失 | 代理先把 `/refund` 转发给 Java，待执行完成后延迟响应，令 Agent 写请求超时 | action 先进入 `UNKNOWN`，只读 result 查询得到 `SUCCEEDED`，最终 action 为 `SUCCEEDED`；写请求只有一次 | PASS：`resultCode=REFUND_SUCCEEDED`，订单 `CLOSE`，`notify_task=1`，`/refund=1` | `test_7_lost_response_reconciles_success_without_second_write` |
| 请求尚未送达 Java | 代理在转发 `/refund` 前阻塞，使 Agent 超时并先发起 result 查询 | Java 为该 key 建立 `ABANDONED`；迟到 `/refund` 不执行；action 为 `FAILED` | PASS：`resultCode=NOT_RECEIVED_BEFORE_QUERY`，订单保持 `CREATE`，`notify_task=0`，`/refund=1` | `test_6_delayed_request_is_abandoned_without_refund` |
| `EXECUTING` 时 kill Agent | 分别在 Java 已执行但未回包、请求尚未转发两个同步点终止 Agent API，再执行 Reconciler | 重启后仅查询 result：已执行场景收敛为 `SUCCEEDED`，未送达场景收敛为 `FAILED` | PASS：两个分支均正确收敛，订单与通知记录一致，每个 action 的 `/refund=1` | `test_9_kill_after_java_execution_reconciles_success`；`test_10_kill_before_java_receives_refund_abandons` |
| 连点两次确认／两个标签页 | 对同一 action 并发调用确认 HTTP 接口 | 只有一个请求获得 `CONFIRMED → EXECUTING` CAS；不产生第二次 Java 写入 | PASS：最终 `SUCCEEDED`，两个响应均未错误显示业务失败，`/refund=1` | `test_4_two_tabs_execute_only_once` |
| 两个 session 对同一订单执行 | 两个 session 各自创建 Proposal 并依次确认 | 第一条 `SUCCEEDED`；第二条 `FAILED/VERSION_CHANGED`；业务只退款一次 | PASS：第二条提示订单已由其他操作关闭，没有重复退款 | `test_8_two_sessions_same_order_have_one_success` |
| Java 宕机时确认 | 代理令 `/refund` 与即时 `/refund/result` 都返回 503；action 成为 `UNKNOWN` 后恢复真实 Java，再运行 Reconciler | 恢复后只读查询 result；不重发 `/refund`；结果正确收敛 | PASS：result 建立 `ABANDONED`，action 为 `FAILED/NOT_RECEIVED_BEFORE_QUERY`，订单 `CREATE`，`notify_task=0`，`/refund=1` | `test_11_java_unavailable_then_result_reconcile_after_recovery` |
| `/refund/result` 一直 `PROCESSING` 或不可用 | 确定性注入连续五次 `UNKNOWN` result | `reconcile_attempts=5`、`needs_manual=1`；用户看到“退款结果暂时无法自动确认，已转人工处理。”；第六次扫描不再查询 Java | PASS：查询恰好五次，第六次扫描数为 0，action 保持 `UNKNOWN` | `test_processing_reaches_manual_and_is_not_queried_again` |
| 聊天中伪造确认凭证 | 在普通聊天中发送 action id、伪造的 64 位 credential，并要求模型直接确认 | 不恢复独立 confirmation workflow；action 不变；不调用 `/refund` | PASS：action 保持 `PROPOSED`、`version=1`，没有执行结果 | `test_13_forged_credential_in_chat_does_not_confirm` |
| 提议过期后确认 | 用已超过五分钟有效期的 Proposal 调确认 HTTP 接口 | HTTP 拒绝；不执行退款 | PASS：HTTP 410，action 保持 `PROPOSED` | `test_3_expired_credential_is_rejected` |
| 用户 B 使用用户 A 的 action id／credential | 用户 B 的 JWT 携带用户 A 的 action id 和有效 credential 调确认接口 | 所有权校验拒绝；不执行退款 | PASS：HTTP 403，未进入确认工作流 | `test_5_cross_user_confirmation_is_rejected` |

## 统一执行入口

在 PowerShell 中，真实写入场景使用正在运行的 agent-dev Java，并且每个场景由测试
调用现有 `reset-agent-dev.ps1` 恢复隔离环境：

```powershell
$env:RUN_C2_INTEGRATION = "1"
$env:RUN_C3_AGENT_DEV = "1"
python -m unittest tests.test_c4_fault_injection -v
```

未设置上述环境标记时，依赖 HTTP/agent-dev 的集成项会明确显示为 skipped，纯确定性
故障项仍可运行。
