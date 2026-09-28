# Agent RAG 规则事实依据（Java V3）

本文档只收录当前 `V3` 分支 Java 源码、MyBatis Mapper 与建表字段能够直接证明的业务事实，供后续 Python RAG 编制规则知识时引用。它不是运行时 Tool 清单，也不扩展源码没有表达的优惠叠加、包邮、发票、积分或支付渠道退款到账规则。

规则总数：**48**。

| 章节 | 数量 |
| --- | ---: |
| 1. 活动状态与有效期 | 6 |
| 2. 用户参与次数、人群资格与流量开关 | 10 |
| 3. 组队、成团与可加入条件 | 8 |
| 4. 订单状态与支付结算 | 6 |
| 5. 超时未支付订单 | 4 |
| 6. 退款预检与人工审核 | 7 |
| 7. 退款执行与到账边界 | 7 |

## 1. 活动状态与有效期（6）

### ACT-001

- rule_id: `ACT-001`
- title: 活动状态枚举
- rule_text: 拼团活动状态只有 `CREATE`（0，创建）、`EFFECTIVE`（1，生效）、`OVERDUE`（2，过期）、`ABANDONED`（3，废弃）四种 Java 枚举值。
- Java 依据：
  - 类 / 方法：`group-buy-market-types/.../ActivityStatusEnumVO.java#valueOf(Integer)`
  - 枚举 / 字段：`ActivityStatusEnumVO.CREATE/EFFECTIVE/OVERDUE/ABANDONED`
  - SQL 条件：`group_buy_activity.status` 映射为该枚举。

### ACT-002

- rule_id: `ACT-002`
- title: 锁单只接受生效活动
- rule_text: 创建或加入拼团并锁定订单前，活动状态必须为 `EFFECTIVE`；其他状态会被活动可用性规则拒绝。
- Java 依据：
  - 类 / 方法：`ActivityUsabilityRuleFilter#apply`
  - 枚举 / 字段：要求 `ActivityStatusEnumVO.EFFECTIVE`，否则抛出 `E0101`
  - SQL 条件：活动由 `group_buy_activity.activity_id` 查询，状态判断在领域规则中完成。

### ACT-003

- rule_id: `ACT-003`
- title: 活动有效期包含起止时刻
- rule_text: 当前时间早于 `startTime` 或晚于 `endTime` 时不可参与；因此恰好等于开始或结束时刻不被该规则判为超出有效期。
- Java 依据：
  - 类 / 方法：`ActivityUsabilityRuleFilter#apply`；`AgentActivityFactsService#getActivityFacts`
  - 枚举 / 字段：`startTime`、`endTime`、`withinValidTime`
  - SQL 条件：时间不是 Mapper 过滤条件，由 Java 使用 `before/after` 比较。

### ACT-004

- rule_id: `ACT-004`
- title: 活动事实查询不把状态和时间混成一个结论
- rule_text: Activity Facts 按 `activityId` 读取活动原始状态、开始和结束时间，并由 Java 独立计算 `withinValidTime`；`status=EFFECTIVE` 不自动等于当前处于有效时间内。
- Java 依据：
  - 类 / 方法：`AgentActivityFactsService#getActivityFacts`；`ActivityRepository#queryGroupBuyActivityFactsSourceByActivityId`
  - 枚举 / 字段：`ActivityFactsVO.status/startTime/endTime/evaluatedAt/withinValidTime`
  - SQL 条件：`queryGroupBuyActivityByActivityId` 仅使用 `where activity_id = #{activityId}`，不附加状态或时间过滤。

### ACT-005

- rule_id: `ACT-005`
- title: 新团队有效期由活动拼团时长产生
- rule_text: 新开团队时，`validStartTime` 取创建时刻，`validEndTime` 等于创建时刻加活动配置的 `validTime` 分钟。
- Java 依据：
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`PayActivityEntity.validTime`、`GroupBuyOrder.validStartTime/validEndTime`
  - SQL 条件：新团队插入 `group_buy_order.valid_start_time/valid_end_time`。

### ACT-006

- rule_id: `ACT-006`
- title: 生效活动配置查询仍需独立时间校验
- rule_text: 按活动 ID 查询可用营销配置时，Mapper 只筛选 `status=1`，没有筛选开始或结束时间；锁单链路随后仍会执行独立的时间范围校验。
- Java 依据：
  - 类 / 方法：`ActivityRepository#queryGroupBuyActivityDiscountVO`；`ActivityUsabilityRuleFilter#apply`
  - 枚举 / 字段：`ActivityStatusEnumVO.EFFECTIVE`
  - SQL 条件：`queryValidGroupBuyActivityId`: `where activity_id = #{activityId} and status = 1`。

## 2. 用户参与次数、人群资格与流量开关（10）

### ELG-001

- rule_id: `ELG-001`
- title: 用户参与次数按活动订单记录计数
- rule_text: 用户在活动中的参与次数，是 `group_buy_order_list` 中该 `userId + activityId` 的记录总数。
- Java 依据：
  - 类 / 方法：`ActivityRepository#queryOrderCountByActivityId`；`TradeRepository#queryOrderCountByActivityId`
  - 枚举 / 字段：`EligibilityFactsVO.userTakeCount`
  - SQL 条件：`select count(id) from group_buy_order_list where user_id = #{userId} and activity_id = #{activityId}`。

### ELG-002

- rule_id: `ELG-002`
- title: 已关闭订单仍计入参与次数
- rule_text: 当前参与次数 SQL 没有状态过滤，所以 `CREATE`、`COMPLETE`、`CLOSE` 订单记录都计入活动参与次数。
- Java 依据：
  - 类 / 方法：`IGroupBuyOrderListDao#queryOrderCountByActivityId`
  - 枚举 / 字段：订单状态字段 `group_buy_order_list.status`
  - SQL 条件：计数 SQL 只有 `user_id` 和 `activity_id` 条件，没有 `status` 条件。

### ELG-003

- rule_id: `ELG-003`
- title: 参与上限达到条件
- rule_text: 当活动 `takeLimitCount` 非空，且用户参与次数大于或等于该上限时，`participationLimitReached=true`。
- Java 依据：
  - 类 / 方法：`AgentEligibilityFactsService#getEligibilityFacts`
  - 枚举 / 字段：`userTakeCount`、`userTakeLimit`、`participationLimitReached`
  - SQL 条件：参与次数来源见 `ELG-001`。

### ELG-004

- rule_id: `ELG-004`
- title: 达到参与上限会阻止锁单
- rule_text: 锁单规则中，活动参与上限非空且计数已达到上限时会抛出 `E0103`，不会继续创建本次拼团订单。
- Java 依据：
  - 类 / 方法：`UserTakeLimitRuleFilter#apply`
  - 枚举 / 字段：`GroupBuyActivityEntity.takeLimitCount`；`ResponseCode.E0103`
  - SQL 条件：使用 `user_id + activity_id` 的订单记录计数。

### ELG-005

- rule_id: `ELG-005`
- title: 未配置人群标签时默认通过标签门槛
- rule_text: 活动 `tagId` 为空时，标签规则视为未配置，标签门槛通过，活动可见且可参与。
- Java 依据：
  - 类 / 方法：`AgentEligibilityFactsService#getEligibilityFacts`；`TagNode#doApply`
  - 枚举 / 字段：`tagRuleConfigured=false`、`tagGatePassed=true`、`tagVisibilityAllowed=true`、`tagParticipationAllowed=true`
  - SQL 条件：`group_buy_activity.tag_id` 为空。

### ELG-006

- rule_id: `ELG-006`
- title: 人群数据可用性取决于 Redis BitSet
- rule_text: 配置了 `tagId` 后，`tagCrowdDataAvailable` 仅表示对应 Redis BitSet 是否存在，不代表当前用户一定属于该人群。
- Java 依据：
  - 类 / 方法：`AgentEligibilityFactsService#getEligibilityFacts`；`ActivityRepository#isTagCrowdDataAvailable`
  - 枚举 / 字段：`EligibilityFactsVO.tagCrowdDataAvailable`
  - SQL 条件：无直接 SQL；读取 `redisService.getBitSet(tagId).isExists()`。

### ELG-007

- rule_id: `ELG-007`
- title: 人群 BitSet 缺失时当前实现按通过处理
- rule_text: `tagId` 已配置但对应 Redis BitSet 不存在时，`isTagCrowdRange` 当前返回 `true`；因此资格事实会同时出现“人群数据不可用”和“标签门槛通过”。这只是当前实现事实，不应推导为已有真实人群成员证据。
- Java 依据：
  - 类 / 方法：`ActivityRepository#isTagCrowdRange`
  - 枚举 / 字段：`tagCrowdDataAvailable=false`、`tagGatePassed=true`
  - SQL 条件：无；Redis BitSet 不存在时直接返回 `true`。

### ELG-008

- rule_id: `ELG-008`
- title: 标签作用域 1 限制可见性
- rule_text: `tagScope` 第一项为 `1` 时，基础可见性为拒绝；属于标签人群的用户可通过 `baseVisible || isWithin` 获得可见性。
- Java 依据：
  - 类 / 方法：`GroupBuyActivityDiscountVO#isVisible`；`TagNode#doApply`
  - 枚举 / 字段：`TagScopeEnumVO.VISIBLE`；`group_buy_activity.tag_scope`
  - SQL 条件：无；Java 解析 `tagScope`。

### ELG-009

- rule_id: `ELG-009`
- title: 标签作用域 2 限制参与资格
- rule_text: `tagScope` 仅为 `2`，或第二项为 `2` 时，基础参与权限为拒绝；属于标签人群的用户可通过 `baseEnable || isWithin` 获得参与权限。
- Java 依据：
  - 类 / 方法：`GroupBuyActivityDiscountVO#isEnable`；`TagNode#doApply`
  - 枚举 / 字段：`TagScopeEnumVO.ENABLE`；`group_buy_activity.tag_scope`
  - SQL 条件：无；Java 解析 `tagScope`。

### ELG-010

- rule_id: `ELG-010`
- title: 降级与切量是独立的参与前置条件
- rule_text: 营销试算链路在降级开关开启时以 `E0003` 拒绝；未进入用户切量范围时以 `E0004` 拒绝。切量使用 `abs(userId.hashCode) % 100 <= cutRange` 判断。
- Java 依据：
  - 类 / 方法：`SwitchNode#doApply`；`DCCService#isDowngradeSwitch/isCutRange`
  - 枚举 / 字段：DCC `downgradeSwitch`（默认 0）、`cutRange`（默认 100）
  - SQL 条件：无；来源为动态配置与用户 ID 哈希。

## 3. 组队、成团与可加入条件（8）

### TEAM-001

- rule_id: `TEAM-001`
- title: 团队状态枚举
- rule_text: 团队状态为 `PROGRESS`（0，拼单中）、`COMPLETE`（1，完成）、`FAIL`（2，失败）、`COMPLETE_FAIL`（3，完成但含退单）。
- Java 依据：
  - 类 / 方法：`GroupBuyOrderEnumVO#valueOf(Integer)`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.PROGRESS/COMPLETE/FAIL/COMPLETE_FAIL`
  - SQL 条件：`group_buy_order.status` 映射为该枚举。

### TEAM-002

- rule_id: `TEAM-002`
- title: 新团队的初始人数与状态
- rule_text: 新开团队时 `completeCount=0`、`lockCount=1`、`targetCount` 来自活动目标人数，数据库插入状态固定为 `PROGRESS(0)`。
- Java 依据：
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`GroupBuyOrder.completeCount/lockCount/targetCount/status`
  - SQL 条件：`group_buy_order_mapper.xml#insert` 将 `status` 写为 0。

### TEAM-003

- rule_id: `TEAM-003`
- title: 数据库层加入团队不能超过目标人数
- rule_text: 加入已有团队时，数据库只在 `lock_count < target_count` 时把锁单人数加 1；更新行数不为 1 会被视为团队已满或不可更新。
- Java 依据：
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`lockCount`、`targetCount`；失败码 `E0005`
  - SQL 条件：`update group_buy_order set lock_count=lock_count+1 where team_id=#{teamId} and lock_count < target_count`。

### TEAM-004

- rule_id: `TEAM-004`
- title: 已有团队先占用 Redis 团队库存
- rule_text: 指定 `teamId` 加团时会按活动目标人数和拼团时长占用 Redis 团队库存；新开团因尚无 `teamId`，跳过该已有团队库存占用步骤。
- Java 依据：
  - 类 / 方法：`TeamStockOccupyRuleFilter#apply`；`TradeRepository#occupyTeamStock`
  - 枚举 / 字段：`target`、`validTime`、`teamStockKey/recoveryTeamStockKey`
  - SQL 条件：无；这是 Redis 原子库存约束。

### TEAM-005

- rule_id: `TEAM-005`
- title: 候选团队必须进行中、有空位且未过团队有效期
- rule_text: Joinable Team 候选的团队记录必须同时满足 `status=PROGRESS`、`target_count > lock_count`、`valid_end_time > now()`。
- Java 依据：
  - 类 / 方法：`ActivityRepository#queryInProgressUserGroupBuyOrderDetailListByRandom`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.PROGRESS`、`targetCount/lockCount/validEndTime`
  - SQL 条件：`group_buy_order_mapper.xml#queryGroupBuyProgressByTeamIds` 的 `status = 0 and target_count > lock_count and valid_end_time > now()`。

### TEAM-006

- rule_id: `TEAM-006`
- title: Joinable Team 不返回当前用户自己的队伍记录
- rule_text: 随机候选来源排除 `user_id = authenticatedUserId`，且只考虑订单状态为 `CREATE` 或 `COMPLETE`、订单 `end_time` 尚未到期并属于进行中团队的记录。
- Java 依据：
  - 类 / 方法：`ActivityRepository#queryInProgressUserGroupBuyOrderDetailListByRandom`
  - 枚举 / 字段：订单状态 0/1；调用参数使用认证用户 ID
  - SQL 条件：`user_id != #{userId}`、`status in (0,1)`、`end_time > now()`、团队子查询 `status=0`。

### TEAM-007

- rule_id: `TEAM-007`
- title: Agent 最多返回两个随机可加入团队
- rule_text: Joinable Team Facts 不返回用户自己的团队，候选上限固定为 2；仓储先最多取 4 条候选记录，随机打乱后截取 2 条，再进行团队有效性过滤，因此最终也可能少于 2 条。
- Java 依据：
  - 类 / 方法：`AgentJoinableTeamFactsService#getJoinableTeamFacts`；`ActivityRepository#queryInProgressUserGroupBuyOrderDetailListByRandom`
  - 枚举 / 字段：`CANDIDATE_TEAM_LIMIT=2`、`ownerCount=0`
  - SQL 条件：订单候选 `limit randomCount * 2`，团队有效性条件见 `TEAM-005`。

### TEAM-008

- rule_id: `TEAM-008`
- title: 团队统计口径不是可加入候选口径
- rule_text: 团队统计先从活动下状态为 0/1 的订单记录按 `team_id` 分组得到团队集合，再统计团队总数、状态为完成的团队数和这些团队的 `lock_count` 总和；该统计没有团队有效期或剩余名额过滤。
- Java 依据：
  - 类 / 方法：`ActivityRepository#queryTeamStatisticByActivityId`
  - 枚举 / 字段：`TeamStatisticVO.allTeamCount/allTeamCompleteCount/allTeamUserCount`
  - SQL 条件：`queryInProgressUserGroupBuyOrderDetailListByActivityId`、`queryAllTeamCount`、`queryAllTeamCompleteCount(status=1)`、`queryAllUserCount(sum(lock_count))`。

## 4. 订单状态与支付结算（6）

### ORD-001

- rule_id: `ORD-001`
- title: 拼团订单明细状态枚举
- rule_text: 用户订单状态为 `CREATE`（0，初始创建）、`COMPLETE`（1，消费完成）、`CLOSE`（2，用户退单）。
- Java 依据：
  - 类 / 方法：`TradeOrderStatusEnumVO#valueOf(Integer)`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE/COMPLETE/CLOSE`
  - SQL 条件：`group_buy_order_list.status` 映射为该枚举。

### ORD-002

- rule_id: `ORD-002`
- title: 锁单创建 CREATE 订单
- rule_text: 锁定营销订单时新增的 `group_buy_order_list` 记录状态为 `CREATE`；此时只是订单已创建，不表示支付完成。
- Java 依据：
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE`
  - SQL 条件：插入 `group_buy_order_list.status=0`。

### ORD-003

- rule_id: `ORD-003`
- title: 用户订单事实按用户和外部交易号联合查询
- rule_text: Order Facts、退款预检和退款数据加载都通过 `userId + outTradeNo` 定位订单；仅有交易号不能读取其他用户订单。
- Java 依据：
  - 类 / 方法：`TradeRepository#queryMarketPayOrderEntityByOutTradeNo`；`OrderFactsService#getOrderFacts`；`RefundPreviewService#getRefundPreview`
  - 枚举 / 字段：`MarketPayOrderEntity.teamId/orderId/status/updateTime`
  - SQL 条件：`where out_trade_no = #{outTradeNo} and user_id = #{userId}`。

### ORD-004

- rule_id: `ORD-004`
- title: 不存在或已关闭订单不能进行支付结算
- rule_text: 支付结算前若订单不存在或已是 `CLOSE`，结算规则以 `E0104` 拒绝。
- Java 依据：
  - 类 / 方法：`OutTradeNoRuleFilter#apply`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CLOSE`；`ResponseCode.E0104`
  - SQL 条件：订单先按 `userId + outTradeNo` 查询。

### ORD-005

- rule_id: `ORD-005`
- title: 支付结算只把 CREATE 更新为 COMPLETE
- rule_text: 支付结算把订单从 `CREATE` 更新为 `COMPLETE` 并写入支付时间；只有原状态为 0 时更新成功，更新行数不为 1 会使事务失败。
- Java 依据：
  - 类 / 方法：`TradeRepository#settlementMarketPayOrder`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE/COMPLETE`、`outTradeTime`
  - SQL 条件：`where out_trade_no=#{outTradeNo} and user_id=#{userId} and status=0`，设置 `status=1`。

### ORD-006

- rule_id: `ORD-006`
- title: 最后一笔支付完成团队
- rule_text: 每次支付结算先将团队 `complete_count` 加 1；当结算前 `targetCount - completeCount == 1` 时，再把进行中的团队状态更新为 `COMPLETE` 并创建成团通知任务。
- Java 依据：
  - 类 / 方法：`TradeRepository#settlementMarketPayOrder`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.PROGRESS/COMPLETE`、`targetCount/completeCount`
  - SQL 条件：完成数更新要求 `complete_count < target_count`；团队完成更新要求 `team_id=#{teamId} and status=0`。

## 5. 超时未支付订单（4）

### TMO-001

- rule_id: `TMO-001`
- title: 超时扫描只选择未支付 CREATE 订单
- rule_text: 超时扫描仅选择订单状态为 `CREATE` 且 `out_trade_time is null` 的订单。
- Java 依据：
  - 类 / 方法：`TradeRefundOrderService#queryTimeoutUnpaidOrderList`；`TradeRepository#queryTimeoutUnpaidOrderList`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE`、`outTradeTime`
  - SQL 条件：`ol.status = 0 and ol.out_trade_time is null`。

### TMO-002

- rule_id: `TMO-002`
- title: 超时判断使用团队有效结束时间
- rule_text: 当前生效的超时 SQL 使用 `group_buy_order.valid_end_time` 判断是否超时，不使用订单明细自己的 `end_time`。
- Java 依据：
  - 类 / 方法：`IGroupBuyOrderListDao#queryTimeoutUnpaidOrderList`
  - 枚举 / 字段：`GroupBuyOrder.validEndTime`
  - SQL 条件：订单明细内连接团队，要求 `now() > gbo.valid_end_time`。

### TMO-003

- rule_id: `TMO-003`
- title: 单批超时扫描上限为十条
- rule_text: 每次超时未支付订单查询最多返回 10 条。
- Java 依据：
  - 类 / 方法：`IGroupBuyOrderListDao#queryTimeoutUnpaidOrderList`
  - 枚举 / 字段：无
  - SQL 条件：查询末尾 `limit 10`。

### TMO-004

- rule_id: `TMO-004`
- title: 超时任务复用真实退款服务并受任务开关控制
- rule_text: 超时任务取得分布式锁后逐条调用 `tradeRefundOrderService.refundOrder`；任务由 `group-buy-market.jobs.timeout-refund.enabled` 控制，缺省为启用，调度表达式为每分钟一次。
- Java 依据：
  - 类 / 方法：`TimeoutRefundJob#exec`
  - 枚举 / 字段：Redis 锁 `group_buy_market_timeout_refund_job_exec`；`@ConditionalOnProperty(matchIfMissing=true)`
  - SQL 条件：超时选择条件见 `TMO-001/TMO-002`。

## 6. 退款预检与人工审核（7）

### PRE-001

- rule_id: `PRE-001`
- title: 退款预检是只读操作
- rule_text: Refund Preview 只读取当前用户订单及其团队，计算退款类型、是否允许生成提议和是否需要人工审核；该服务不调用退款执行、消息、缓存写或仓储写方法。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#getRefundPreview/buildPreview`
  - 枚举 / 字段：`RefundPreviewVO`
  - SQL 条件：只调用订单与团队 `SELECT`。

### PRE-002

- rule_id: `PRE-002`
- title: 已关闭订单不再生成退款提议
- rule_text: 订单状态为 `CLOSE` 时，预检返回 `refundType=null`、`refundProposalAllowed=false`、`requiresManualReview=false`。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CLOSE`
  - SQL 条件：订单来自 `userId + outTradeNo` 查询。

### PRE-003

- rule_id: `PRE-003`
- title: UNPAID 预检组合
- rule_text: 订单为 `CREATE` 且团队为 `PROGRESS` 时，预检类型为 `UNPAID`，允许生成退款提议且不要求人工审核。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE`、`GroupBuyOrderEnumVO.PROGRESS`
  - SQL 条件：无额外 SQL；由已读取状态组合确定。

### PRE-004

- rule_id: `PRE-004`
- title: PAID_UNFORMED 预检组合
- rule_text: 订单为 `COMPLETE` 且团队为 `PROGRESS` 时，预检类型为 `PAID_UNFORMED`，允许生成退款提议且不要求人工审核。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.COMPLETE`、`GroupBuyOrderEnumVO.PROGRESS`
  - SQL 条件：无额外 SQL；由已读取状态组合确定。

### PRE-005

- rule_id: `PRE-005`
- title: PAID_FORMED 必须人工审核
- rule_text: 订单为 `COMPLETE`，团队为 `COMPLETE` 或 `COMPLETE_FAIL` 时，预检类型为 `PAID_FORMED`；可以形成提议，但 `requiresManualReview=true`，Agent 退款协调器不会自动执行。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#buildPreview`；`AgentRefundService#refund`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.COMPLETE`、`GroupBuyOrderEnumVO.COMPLETE/COMPLETE_FAIL`、结果码 `MANUAL_REVIEW_REQUIRED`
  - SQL 条件：无额外 SQL；由状态组合确定。

### PRE-006

- rule_id: `PRE-006`
- title: 未识别或终态组合禁止自动提议
- rule_text: 除明确的三种状态组合和已关闭订单外，其余组合返回 `refundType=null`、`refundProposalAllowed=false`、`requiresManualReview=true`。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：退款预检默认分支
  - SQL 条件：无额外 SQL。

### PRE-007

- rule_id: `PRE-007`
- title: 退款预检版本由订单与团队共同组成
- rule_text: 预检分别返回订单和团队真实 `updateTime`；Agent 执行前把二者格式化并拼为 `orderUpdateTime|teamUpdateTime`，任一版本或预期退款类型变化都会返回 `VERSION_CHANGED`，不调用真实退款服务。
- Java 依据：
  - 类 / 方法：`RefundPreviewService#response`；`AgentRefundService#versionOf/refund`
  - 枚举 / 字段：`RefundPreviewVO.orderUpdateTime/teamUpdateTime/refundType`
  - SQL 条件：订单和团队查询都读取各自 `update_time` 字段。

## 7. 退款执行与到账边界（7）

### RFD-001

- rule_id: `RFD-001`
- title: 真实退款策略按订单与团队状态联合选择
- rule_text: 执行退款时，`CREATE + PROGRESS` 选择未支付未成团策略，`COMPLETE + PROGRESS` 选择已支付未成团策略，`COMPLETE + (COMPLETE 或 COMPLETE_FAIL)` 选择已支付已成团策略；其他组合不受支持。
- Java 依据：
  - 类 / 方法：`RefundTypeEnumVO#getRefundStrategy`；`RefundOrderNodeFilter#apply`
  - 枚举 / 字段：`UNPAID_UNLOCK`、`PAID_UNFORMED`、`PAID_FORMED`
  - SQL 条件：状态来自订单和团队查询。

### RFD-002

- rule_id: `RFD-002`
- title: CLOSE 订单在退款执行链中按重复退单处理
- rule_text: 退款数据加载后若订单已经为 `CLOSE`，重复退单过滤器返回 `REPEAT`，不再进入退款策略执行。
- Java 依据：
  - 类 / 方法：`UniqueRefundNodeFilter#apply`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CLOSE`、`TradeRefundBehaviorEnum.REPEAT`
  - SQL 条件：订单按 `userId + outTradeNo` 读取当前状态。

### RFD-003

- rule_id: `RFD-003`
- title: UNPAID 退款的数据库状态变化
- rule_text: 未支付未成团退款把当前用户订单从 `CREATE` 更新为 `CLOSE`，团队 `lock_count` 减 1，并创建退款通知任务；团队更新只允许在 `PROGRESS` 状态执行。
- Java 依据：
  - 类 / 方法：`Unpaid2RefundStrategy#refundOrder`；`TradeRepository#unpaid2Refund`
  - 枚举 / 字段：`RefundTypeEnumVO.UNPAID_UNLOCK`；团队增量 `lockCount=-1`
  - SQL 条件：订单 `user_id + order_id + status=0` 更新为 2；团队 `team_id + status=0` 更新 `lock_count=lock_count-1`。

### RFD-004

- rule_id: `RFD-004`
- title: PAID_UNFORMED 退款的数据库状态变化
- rule_text: 已支付未成团退款把当前用户订单从 `COMPLETE` 更新为 `CLOSE`，并在进行中团队上将 `lock_count` 和 `complete_count` 各减 1，同时创建退款通知任务。
- Java 依据：
  - 类 / 方法：`Paid2RefundStrategy#refundOrder`；`TradeRepository#paid2Refund`
  - 枚举 / 字段：`RefundTypeEnumVO.PAID_UNFORMED`；两个人数增量均为 -1
  - SQL 条件：订单 `user_id + order_id + status=1` 更新为 2；团队要求 `team_id + status=0`。

### RFD-005

- rule_id: `RFD-005`
- title: 已支付已成团退款会改变成团结果状态
- rule_text: 已支付已成团退款把订单从 `COMPLETE` 更新为 `CLOSE`，并减少团队锁单数与完成数；退款前完成数大于 1 时团队变为 `COMPLETE_FAIL`，等于 1 时团队变为 `FAIL`。
- Java 依据：
  - 类 / 方法：`PaidTeam2RefundStrategy#refundOrder`；`TradeRepository#paidTeam2Refund`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.COMPLETE_FAIL/FAIL`、`completeCount`
  - SQL 条件：团队原状态须为 1 或 3；`complete_count > 1` 更新状态 3，`complete_count = 1` 更新状态 2。

### RFD-006

- rule_id: `RFD-006`
- title: 退款更新必须各命中一行
- rule_text: 三种退款仓储操作都要求订单更新和团队更新各命中恰好 1 行；否则抛出 `UPDATE_ZERO`。这些操作处于同一事务方法中，因此失败不会被当作成功退款结果。
- Java 依据：
  - 类 / 方法：`TradeRepository#unpaid2Refund/paid2Refund/paidTeam2Refund`，均标注 `@Transactional`
  - 枚举 / 字段：`ResponseCode.UPDATE_ZERO`
  - SQL 条件：各退款 Mapper SQL 都带旧订单或旧团队状态条件。

### RFD-007

- rule_id: `RFD-007`
- title: CLOSE 不能证明支付渠道退款到账
- rule_text: `CLOSE` 只证明 Java 拼团订单明细已进入“用户退单”状态。当前退款策略更新本地订单/团队并创建通知任务，但源码没有支付渠道退款流水号、渠道退款状态、到账金额或到账时间，因此不得把 `CLOSE` 解释为“资金已退回并到账”。
- Java 依据：
  - 类 / 方法：`TradeOrderStatusEnumVO`；`TradeRepository#unpaid2Refund/paid2Refund/paidTeam2Refund`
  - 枚举 / 字段：`CLOSE(2, "用户退单")`；本地通知字段不等同于渠道退款回执
  - SQL 条件：退款 SQL 只更新 `group_buy_order_list`、`group_buy_order` 并插入 `notify_task`；没有渠道退款记录表或到账字段写入。

## 使用边界

- 上述规则是当前 Java V3 的实现事实，不应扩写为源码未证明的售后、资金到账或商品履约承诺。
- Facts 中的布尔值应保持各自含义。例如 `tagCrowdDataAvailable=false` 与 `tagGatePassed=true` 可以同时出现，RAG 不应把后者改写成“用户已被真实人群数据命中”。
- `team statistics` 与 `candidateTeams` 的 SQL 口径不同，不能用统计总量推导某个团队当前一定可加入。
- `CLOSE`、退款通知已创建、MQ 已发送，均不能单独证明外部支付渠道退款到账。
