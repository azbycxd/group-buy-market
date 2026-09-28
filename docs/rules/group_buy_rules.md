# 拼团客服业务规则知识库

本文件基于 `docs/rules/java_rule_source.md` 改写。`content` 仅表达用户可理解的业务规则；`source` 只用于追溯 Java 依据，不参与检索，也不得直接提供给用户。

## ACT-001
- rule_id: `ACT-001`
- section: 活动状态与有效期
- title: 活动可能处于哪些状态
- content: 活动可能处于创建、生效、过期或已废弃状态；只有处于生效状态的活动才具备继续参与的基础条件。
- visibility: public
- source:
  - 类 / 方法：`group-buy-market-types/.../ActivityStatusEnumVO.java#valueOf(Integer)`
  - 枚举 / 字段：`ActivityStatusEnumVO.CREATE/EFFECTIVE/OVERDUE/ABANDONED`
  - SQL 条件：`group_buy_activity.status` 映射为该枚举。

## ACT-002
- rule_id: `ACT-002`
- section: 活动状态与有效期
- title: 只有生效活动才能参团
- content: 活动尚未生效、已经过期或已经废弃时，不能创建或加入拼团。
- visibility: public
- source:
  - 类 / 方法：`ActivityUsabilityRuleFilter#apply`
  - 枚举 / 字段：要求 `ActivityStatusEnumVO.EFFECTIVE`，否则抛出 `E0101`
  - SQL 条件：活动由 `group_buy_activity.activity_id` 查询，状态判断在领域规则中完成。

## ACT-003
- rule_id: `ACT-003`
- section: 活动状态与有效期
- title: 活动起止时刻计入有效期
- content: 恰好在活动开始或结束时刻仍视为处于有效期；早于开始时间或晚于结束时间则不能参加。
- visibility: public
- source:
  - 类 / 方法：`ActivityUsabilityRuleFilter#apply`；`AgentActivityFactsService#getActivityFacts`
  - 枚举 / 字段：`startTime`、`endTime`、`withinValidTime`
  - SQL 条件：时间不是 Mapper 过滤条件，由 Java 使用 `before/after` 比较。

## ACT-004
- rule_id: `ACT-004`
- section: 活动状态与有效期
- title: 活动生效不等于当前可参加
- content: 活动状态显示为生效，只说明活动没有被停用；当前能否参加还要单独确认是否处于活动开放时间内。
- visibility: public
- source:
  - 类 / 方法：`AgentActivityFactsService#getActivityFacts`；`ActivityRepository#queryGroupBuyActivityFactsSourceByActivityId`
  - 枚举 / 字段：`ActivityFactsVO.status/startTime/endTime/evaluatedAt/withinValidTime`
  - SQL 条件：`queryGroupBuyActivityByActivityId` 仅使用 `where activity_id = #{activityId}`，不附加状态或时间过滤。

## ACT-005
- rule_id: `ACT-005`
- section: 活动状态与有效期
- title: 新团截止时间如何确定
- content: 新团从创建时开始计时，截止时间由活动设置的拼团时长决定。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`PayActivityEntity.validTime`、`GroupBuyOrder.validStartTime/validEndTime`
  - SQL 条件：新团队插入 `group_buy_order.valid_start_time/valid_end_time`。

## ACT-006
- rule_id: `ACT-006`
- section: 活动状态与有效期
- title: 活动可用性需要完整校验
- content: 系统找到生效活动后，仍会继续确认当前时间是否处于活动期限内。
- visibility: internal
- source:
  - 类 / 方法：`ActivityRepository#queryGroupBuyActivityDiscountVO`；`ActivityUsabilityRuleFilter#apply`
  - 枚举 / 字段：`ActivityStatusEnumVO.EFFECTIVE`
  - SQL 条件：`queryValidGroupBuyActivityId`: `where activity_id = #{activityId} and status = 1`。

## ELG-001
- rule_id: `ELG-001`
- section: 用户参与次数、人群资格与流量开关
- title: 参与次数如何计算
- content: 同一用户在同一活动下产生的每一笔拼团订单，都会计入该活动的参与次数。
- visibility: public
- source:
  - 类 / 方法：`ActivityRepository#queryOrderCountByActivityId`；`TradeRepository#queryOrderCountByActivityId`
  - 枚举 / 字段：`EligibilityFactsVO.userTakeCount`
  - SQL 条件：`select count(id) from group_buy_order_list where user_id = #{userId} and activity_id = #{activityId}`。

## ELG-002
- rule_id: `ELG-002`
- section: 用户参与次数、人群资格与流量开关
- title: 已关闭订单仍占参与次数
- content: 已经取消或关闭的拼团订单仍会计入活动参与次数，不会因为订单关闭而自动返还次数。
- visibility: public
- source:
  - 类 / 方法：`IGroupBuyOrderListDao#queryOrderCountByActivityId`
  - 枚举 / 字段：订单状态字段 `group_buy_order_list.status`
  - SQL 条件：计数 SQL 只有 `user_id` 和 `activity_id` 条件，没有 `status` 条件。

## ELG-003
- rule_id: `ELG-003`
- section: 用户参与次数、人群资格与流量开关
- title: 何时算参与次数已用完
- content: 活动设置了参与上限时，用户已参与次数达到或超过上限，就视为次数已经用完。
- visibility: public
- source:
  - 类 / 方法：`AgentEligibilityFactsService#getEligibilityFacts`
  - 枚举 / 字段：`userTakeCount`、`userTakeLimit`、`participationLimitReached`
  - SQL 条件：参与次数来源见 `ELG-001`。

## ELG-004
- rule_id: `ELG-004`
- section: 用户参与次数、人群资格与流量开关
- title: 次数用完后不能继续参团
- content: 用户的活动参与次数达到上限后，不能再创建或加入新的拼团订单。
- visibility: public
- source:
  - 类 / 方法：`UserTakeLimitRuleFilter#apply`
  - 枚举 / 字段：`GroupBuyActivityEntity.takeLimitCount`；`ResponseCode.E0103`
  - SQL 条件：使用 `user_id + activity_id` 的订单记录计数。

## ELG-005
- rule_id: `ELG-005`
- section: 用户参与次数、人群资格与流量开关
- title: 没有人群限制时默认可以参加
- content: 活动没有设置专属人群时，普通用户默认可以看到并参与该活动，但仍需满足活动时间和次数等其他条件。
- visibility: public
- source:
  - 类 / 方法：`AgentEligibilityFactsService#getEligibilityFacts`；`TagNode#doApply`
  - 枚举 / 字段：`tagRuleConfigured=false`、`tagGatePassed=true`、`tagVisibilityAllowed=true`、`tagParticipationAllowed=true`
  - SQL 条件：`group_buy_activity.tag_id` 为空。

## ELG-006
- rule_id: `ELG-006`
- section: 用户参与次数、人群资格与流量开关
- title: 人群资格数据是否可用
- content: 系统会把人群规则是否配置、人群资格数据是否可用以及用户是否满足条件分别判断。
- visibility: internal
- source:
  - 类 / 方法：`AgentEligibilityFactsService#getEligibilityFacts`；`ActivityRepository#isTagCrowdDataAvailable`
  - 枚举 / 字段：`EligibilityFactsVO.tagCrowdDataAvailable`
  - SQL 条件：无直接 SQL；读取 `redisService.getBitSet(tagId).isExists()`。

## ELG-007
- rule_id: `ELG-007`
- section: 用户参与次数、人群资格与流量开关
- title: 人群数据缺失时的内部兜底
- content: 人群资格数据暂时缺失时，系统当前可能按通过处理；这种兜底不能证明用户确实属于目标人群。
- visibility: internal
- source:
  - 类 / 方法：`ActivityRepository#isTagCrowdRange`
  - 枚举 / 字段：`tagCrowdDataAvailable=false`、`tagGatePassed=true`
  - SQL 条件：无；Redis BitSet 不存在时直接返回 `true`。

## ELG-008
- rule_id: `ELG-008`
- section: 用户参与次数、人群资格与流量开关
- title: 部分活动仅对指定人群可见
- content: 活动可以限制展示范围；不属于指定人群的用户可能看不到活动入口。
- visibility: public
- source:
  - 类 / 方法：`GroupBuyActivityDiscountVO#isVisible`；`TagNode#doApply`
  - 枚举 / 字段：`TagScopeEnumVO.VISIBLE`；`group_buy_activity.tag_scope`
  - SQL 条件：无；Java 解析 `tagScope`。

## ELG-009
- rule_id: `ELG-009`
- section: 用户参与次数、人群资格与流量开关
- title: 部分活动仅允许指定人群参加
- content: 活动可以限制参与资格；用户即使看得到活动，也可能因为不属于指定人群而不能参团。
- visibility: public
- source:
  - 类 / 方法：`GroupBuyActivityDiscountVO#isEnable`；`TagNode#doApply`
  - 枚举 / 字段：`TagScopeEnumVO.ENABLE`；`group_buy_activity.tag_scope`
  - SQL 条件：无；Java 解析 `tagScope`。

## ELG-010
- rule_id: `ELG-010`
- section: 用户参与次数、人群资格与流量开关
- title: 系统保护措施可能暂时阻止参与
- content: 系统维护或流量保护期间，部分用户的活动试算可能被暂时拒绝。
- visibility: internal
- source:
  - 类 / 方法：`SwitchNode#doApply`；`DCCService#isDowngradeSwitch/isCutRange`
  - 枚举 / 字段：DCC `downgradeSwitch`（默认 0）、`cutRange`（默认 100）
  - SQL 条件：无；来源为动态配置与用户 ID 哈希。

## TEAM-001
- rule_id: `TEAM-001`
- section: 组队、成团与可加入条件
- title: 拼团队伍可能处于哪些状态
- content: 拼团队伍可能处于拼团中、已成团、拼团失败或成团后发生退单等状态。
- visibility: public
- source:
  - 类 / 方法：`GroupBuyOrderEnumVO#valueOf(Integer)`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.PROGRESS/COMPLETE/FAIL/COMPLETE_FAIL`
  - SQL 条件：`group_buy_order.status` 映射为该枚举。

## TEAM-002
- rule_id: `TEAM-002`
- section: 组队、成团与可加入条件
- title: 新团创建后的初始状态
- content: 新团创建后处于拼团中，发起人会先占用一个参团名额，已完成付款人数从零开始计算。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`GroupBuyOrder.completeCount/lockCount/targetCount/status`
  - SQL 条件：`group_buy_order_mapper.xml#insert` 将 `status` 写为 0。

## TEAM-003
- rule_id: `TEAM-003`
- section: 组队、成团与可加入条件
- title: 满员团队不能继续加入
- content: 团队剩余名额用完后不能再加入；系统不会让实际占用名额超过目标人数。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`lockCount`、`targetCount`；失败码 `E0005`
  - SQL 条件：`update group_buy_order set lock_count=lock_count+1 where team_id=#{teamId} and lock_count < target_count`。

## TEAM-004
- rule_id: `TEAM-004`
- section: 组队、成团与可加入条件
- title: 加入已有团队前会预留名额
- content: 加入已有团队时，系统会先确认并预留可用名额，以避免同一名额被重复占用。
- visibility: internal
- source:
  - 类 / 方法：`TeamStockOccupyRuleFilter#apply`；`TradeRepository#occupyTeamStock`
  - 枚举 / 字段：`target`、`validTime`、`teamStockKey/recoveryTeamStockKey`
  - SQL 条件：无；这是 Redis 原子库存约束。

## TEAM-005
- rule_id: `TEAM-005`
- section: 组队、成团与可加入条件
- title: 什么团队可以继续加入
- content: 可加入团队必须仍在拼团中、还有剩余名额，并且没有超过该团队的截止时间。
- visibility: public
- source:
  - 类 / 方法：`ActivityRepository#queryInProgressUserGroupBuyOrderDetailListByRandom`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.PROGRESS`、`targetCount/lockCount/validEndTime`
  - SQL 条件：`group_buy_order_mapper.xml#queryGroupBuyProgressByTeamIds` 的 `status = 0 and target_count > lock_count and valid_end_time > now()`。

## TEAM-006
- rule_id: `TEAM-006`
- section: 组队、成团与可加入条件
- title: 可加入列表不会推荐自己的队伍
- content: 系统提供可加入团队时，会排除当前用户自己创建或已经参加的队伍。
- visibility: public
- source:
  - 类 / 方法：`ActivityRepository#queryInProgressUserGroupBuyOrderDetailListByRandom`
  - 枚举 / 字段：订单状态 0/1；调用参数使用认证用户 ID
  - SQL 条件：`user_id != #{userId}`、`status in (0,1)`、`end_time > now()`、团队子查询 `status=0`。

## TEAM-007
- rule_id: `TEAM-007`
- section: 组队、成团与可加入条件
- title: 可加入团队最多展示两个
- content: 系统一次最多展示两个可加入团队；如果符合条件的团队不足，实际展示数量会更少。
- visibility: public
- source:
  - 类 / 方法：`AgentJoinableTeamFactsService#getJoinableTeamFacts`；`ActivityRepository#queryInProgressUserGroupBuyOrderDetailListByRandom`
  - 枚举 / 字段：`CANDIDATE_TEAM_LIMIT=2`、`ownerCount=0`
  - SQL 条件：订单候选 `limit randomCount * 2`，团队有效性条件见 `TEAM-005`。

## TEAM-008
- rule_id: `TEAM-008`
- section: 组队、成团与可加入条件
- title: 团队统计不等于可加入数量
- content: 活动页面的团队总数是统计口径，不代表这些团队当前都有空位或仍在有效期内，因此不能直接当作可加入团队数量。
- visibility: public
- source:
  - 类 / 方法：`ActivityRepository#queryTeamStatisticByActivityId`
  - 枚举 / 字段：`TeamStatisticVO.allTeamCount/allTeamCompleteCount/allTeamUserCount`
  - SQL 条件：`queryInProgressUserGroupBuyOrderDetailListByActivityId`、`queryAllTeamCount`、`queryAllTeamCompleteCount(status=1)`、`queryAllUserCount(sum(lock_count))`。

## ORD-001
- rule_id: `ORD-001`
- section: 订单状态与支付结算
- title: 拼团订单可能处于哪些状态
- content: 用户拼团订单可能处于待支付、已支付完成或已关闭退单状态。
- visibility: public
- source:
  - 类 / 方法：`TradeOrderStatusEnumVO#valueOf(Integer)`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE/COMPLETE/CLOSE`
  - SQL 条件：`group_buy_order_list.status` 映射为该枚举。

## ORD-002
- rule_id: `ORD-002`
- section: 订单状态与支付结算
- title: 刚参团的订单仍待支付
- content: 创建或加入拼团后，新订单先处于待支付状态；订单创建成功不代表已经付款。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#lockMarketPayOrder`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE`
  - SQL 条件：插入 `group_buy_order_list.status=0`。

## ORD-003
- rule_id: `ORD-003`
- section: 订单状态与支付结算
- title: 订单查询受用户身份保护
- content: 查询订单时会同时核对登录用户和交易号；只知道交易号也不能查看其他用户的订单。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#queryMarketPayOrderEntityByOutTradeNo`；`OrderFactsService#getOrderFacts`；`RefundPreviewService#getRefundPreview`
  - 枚举 / 字段：`MarketPayOrderEntity.teamId/orderId/status/updateTime`
  - SQL 条件：`where out_trade_no = #{outTradeNo} and user_id = #{userId}`。

## ORD-004
- rule_id: `ORD-004`
- section: 订单状态与支付结算
- title: 已关闭订单不能继续支付
- content: 订单不存在或已经关闭时，不能再进行支付结算。
- visibility: public
- source:
  - 类 / 方法：`OutTradeNoRuleFilter#apply`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CLOSE`；`ResponseCode.E0104`
  - SQL 条件：订单先按 `userId + outTradeNo` 查询。

## ORD-005
- rule_id: `ORD-005`
- section: 订单状态与支付结算
- title: 付款完成后订单状态更新
- content: 待支付订单完成付款后会变为已支付完成，并记录付款时间；已经不是待支付状态的订单不能重复结算。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#settlementMarketPayOrder`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE/COMPLETE`、`outTradeTime`
  - SQL 条件：`where out_trade_no=#{outTradeNo} and user_id=#{userId} and status=0`，设置 `status=1`。

## ORD-006
- rule_id: `ORD-006`
- section: 订单状态与支付结算
- title: 最后一人付款后完成拼团
- content: 当最后一个所需成员完成付款后，团队会变为已成团，并进入后续成团通知流程。
- visibility: public
- source:
  - 类 / 方法：`TradeRepository#settlementMarketPayOrder`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.PROGRESS/COMPLETE`、`targetCount/completeCount`
  - SQL 条件：完成数更新要求 `complete_count < target_count`；团队完成更新要求 `team_id=#{teamId} and status=0`。

## TMO-001
- rule_id: `TMO-001`
- section: 超时未支付订单
- title: 超时处理只针对未付款订单
- content: 系统的超时处理只针对仍处于待支付且没有付款记录的订单，已付款订单不会按未支付超时处理。
- visibility: public
- source:
  - 类 / 方法：`TradeRefundOrderService#queryTimeoutUnpaidOrderList`；`TradeRepository#queryTimeoutUnpaidOrderList`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE`、`outTradeTime`
  - SQL 条件：`ol.status = 0 and ol.out_trade_time is null`。

## TMO-002
- rule_id: `TMO-002`
- section: 超时未支付订单
- title: 未付款超时以团队截止时间为准
- content: 未付款订单是否超时，按所属拼团队伍的截止时间判断。
- visibility: public
- source:
  - 类 / 方法：`IGroupBuyOrderListDao#queryTimeoutUnpaidOrderList`
  - 枚举 / 字段：`GroupBuyOrder.validEndTime`
  - SQL 条件：订单明细内连接团队，要求 `now() > gbo.valid_end_time`。

## TMO-003
- rule_id: `TMO-003`
- section: 超时未支付订单
- title: 超时订单分批处理
- content: 系统会分批处理超时未付款订单，每批最多处理十笔。
- visibility: internal
- source:
  - 类 / 方法：`IGroupBuyOrderListDao#queryTimeoutUnpaidOrderList`
  - 枚举 / 字段：无
  - SQL 条件：查询末尾 `limit 10`。

## TMO-004
- rule_id: `TMO-004`
- section: 超时未支付订单
- title: 超时订单由后台定期处理
- content: 系统会定期检查并处理超时未付款订单，该后台处理可以由运维配置启停。
- visibility: internal
- source:
  - 类 / 方法：`TimeoutRefundJob#exec`
  - 枚举 / 字段：Redis 锁 `group_buy_market_timeout_refund_job_exec`；`@ConditionalOnProperty(matchIfMissing=true)`
  - SQL 条件：超时选择条件见 `TMO-001/TMO-002`。

## PRE-001
- rule_id: `PRE-001`
- section: 退款预检与人工审核
- title: 查看退款预览不会执行退款
- content: 退款预览只用于判断当前订单是否可退、属于哪种退款情形以及是否需要人工审核，不会直接修改订单或执行退款。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#getRefundPreview/buildPreview`
  - 枚举 / 字段：`RefundPreviewVO`
  - SQL 条件：只调用订单与团队 `SELECT`。

## PRE-002
- rule_id: `PRE-002`
- section: 退款预检与人工审核
- title: 已关闭订单不能再次生成退款提议
- content: 订单已经关闭时，不会再生成新的可确认退款提议。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CLOSE`
  - SQL 条件：订单来自 `userId + outTradeNo` 查询。

## PRE-003
- rule_id: `PRE-003`
- section: 退款预检与人工审核
- title: 未付款且未成团可以提议取消
- content: 订单尚未付款且团队仍在拼团时，可以生成取消拼团的退款提议，通常不需要人工审核。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CREATE`、`GroupBuyOrderEnumVO.PROGRESS`
  - SQL 条件：无额外 SQL；由已读取状态组合确定。

## PRE-004
- rule_id: `PRE-004`
- section: 退款预检与人工审核
- title: 已付款但未成团可以提议退款
- content: 订单已经付款但团队仍未成团时，可以生成退款提议，通常不需要人工审核。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.COMPLETE`、`GroupBuyOrderEnumVO.PROGRESS`
  - SQL 条件：无额外 SQL；由已读取状态组合确定。

## PRE-005
- rule_id: `PRE-005`
- section: 退款预检与人工审核
- title: 已付款已成团退款需要人工审核
- content: 订单已经付款且团队已经成团时，退款必须由人工审核，系统不会自动执行。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#buildPreview`；`AgentRefundService#refund`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.COMPLETE`、`GroupBuyOrderEnumVO.COMPLETE/COMPLETE_FAIL`、结果码 `MANUAL_REVIEW_REQUIRED`
  - SQL 条件：无额外 SQL；由状态组合确定。

## PRE-006
- rule_id: `PRE-006`
- section: 退款预检与人工审核
- title: 异常状态组合需要人工处理
- content: 订单和团队状态不属于明确支持的退款情形时，系统不会生成可自动确认的退款提议，并会要求人工处理。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#buildPreview`
  - 枚举 / 字段：退款预检默认分支
  - SQL 条件：无额外 SQL。

## PRE-007
- rule_id: `PRE-007`
- section: 退款预检与人工审核
- title: 订单或团队变化后需重新提议
- content: 退款提议生成后，如果订单状态或团队状态发生变化，原提议将不能继续执行，需要重新发起并确认。
- visibility: public
- source:
  - 类 / 方法：`RefundPreviewService#response`；`AgentRefundService#versionOf/refund`
  - 枚举 / 字段：`RefundPreviewVO.orderUpdateTime/teamUpdateTime/refundType`
  - SQL 条件：订单和团队查询都读取各自 `update_time` 字段。

## RFD-001
- rule_id: `RFD-001`
- section: 退款执行与到账边界
- title: 退款方式由付款和成团情况决定
- content: 系统会根据订单是否付款以及团队是否成团，区分未付款取消、已付款未成团退款和已付款已成团退款。
- visibility: public
- source:
  - 类 / 方法：`RefundTypeEnumVO#getRefundStrategy`；`RefundOrderNodeFilter#apply`
  - 枚举 / 字段：`UNPAID_UNLOCK`、`PAID_UNFORMED`、`PAID_FORMED`
  - SQL 条件：状态来自订单和团队查询。

## RFD-002
- rule_id: `RFD-002`
- section: 退款执行与到账边界
- title: 已关闭订单不会重复退款
- content: 订单已经关闭后，再次发起同一退单操作不会重复执行退款。
- visibility: public
- source:
  - 类 / 方法：`UniqueRefundNodeFilter#apply`
  - 枚举 / 字段：`TradeOrderStatusEnumVO.CLOSE`、`TradeRefundBehaviorEnum.REPEAT`
  - SQL 条件：订单按 `userId + outTradeNo` 读取当前状态。

## RFD-003
- rule_id: `RFD-003`
- section: 退款执行与到账边界
- title: 未付款取消会释放参团名额
- content: 未付款时取消拼团后，订单会关闭，之前占用的参团名额会被释放。
- visibility: public
- source:
  - 类 / 方法：`Unpaid2RefundStrategy#refundOrder`；`TradeRepository#unpaid2Refund`
  - 枚举 / 字段：`RefundTypeEnumVO.UNPAID_UNLOCK`；团队增量 `lockCount=-1`
  - SQL 条件：订单 `user_id + order_id + status=0` 更新为 2；团队 `team_id + status=0` 更新 `lock_count=lock_count-1`。

## RFD-004
- rule_id: `RFD-004`
- section: 退款执行与到账边界
- title: 已付款未成团退款会调整团队人数
- content: 已付款但尚未成团时退款，订单会关闭，团队中的占位人数和已付款人数会同步减少。
- visibility: public
- source:
  - 类 / 方法：`Paid2RefundStrategy#refundOrder`；`TradeRepository#paid2Refund`
  - 枚举 / 字段：`RefundTypeEnumVO.PAID_UNFORMED`；两个人数增量均为 -1
  - SQL 条件：订单 `user_id + order_id + status=1` 更新为 2；团队要求 `team_id + status=0`。

## RFD-005
- rule_id: `RFD-005`
- section: 退款执行与到账边界
- title: 成团后退单会影响团队结果
- content: 已经成团后发生退款，订单会关闭，团队结果会根据剩余已完成人数变为成团后有退单或拼团失败。
- visibility: public
- source:
  - 类 / 方法：`PaidTeam2RefundStrategy#refundOrder`；`TradeRepository#paidTeam2Refund`
  - 枚举 / 字段：`GroupBuyOrderEnumVO.COMPLETE_FAIL/FAIL`、`completeCount`
  - SQL 条件：团队原状态须为 1 或 3；`complete_count > 1` 更新状态 3，`complete_count = 1` 更新状态 2。

## RFD-006
- rule_id: `RFD-006`
- section: 退款执行与到账边界
- title: 退款相关状态必须完整更新
- content: 退款涉及的订单和团队状态必须全部更新成功；任一部分失败，本次操作都不能视为退款成功。
- visibility: internal
- source:
  - 类 / 方法：`TradeRepository#unpaid2Refund/paid2Refund/paidTeam2Refund`，均标注 `@Transactional`
  - 枚举 / 字段：`ResponseCode.UPDATE_ZERO`
  - SQL 条件：各退款 Mapper SQL 都带旧订单或旧团队状态条件。

## RFD-007
- rule_id: `RFD-007`
- section: 退款执行与到账边界
- title: 订单关闭不代表退款资金到账
- content: 订单显示已关闭，只能说明拼团订单已经退单，不能证明支付渠道的退款资金已经到账；到账情况需要由支付渠道核实。
- visibility: public
- source:
  - 类 / 方法：`TradeOrderStatusEnumVO`；`TradeRepository#unpaid2Refund/paid2Refund/paidTeam2Refund`
  - 枚举 / 字段：`CLOSE(2, "用户退单")`；本地通知字段不等同于渠道退款回执
  - SQL 条件：退款 SQL 只更新 `group_buy_order_list`、`group_buy_order` 并插入 `notify_task`；没有渠道退款记录表或到账字段写入。
