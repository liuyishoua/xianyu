# 总部业务接口

这些接口复用工作台管理员登录、同源写入检查。总部通过正常登录和 `X-App-Request: 1` 调用，不获取平台 cookies。业务授权属于目标，不受浏览器当前账号影响。它不修改原监听/自动回复开关。

- `GET /api/business/capabilities`：`resources[{account_id,name,operations[{id,label,available,reason,parameters}],scope_options}]`。
- `PUT /api/business/goals/<goal_id>/authority`：`{project_id,revision,enabled,expires_at,grants:[{account_id,operations,scope:{resource_ids,conversation_ids,contact_ids,include_goal_created}}]}`。最长 30 秒，版本相同只能续期，权限变化要递增。所有账号必须已登记。
- `POST /api/business/tasks`：`{origin:"headquarters",request_id,goal_id,project_id,action_id,account_id,operation,input,authorization_revision}`，返回 202 持久任务。相同 request_id/完整参数返回同一个任务；改参数拒绝。原平台 job 标识由服务器生成，不能伪造或替换。模型 message_id 固定为 business- 加总部 request_id，资源目录提供真实 channel_source_id/channel_account_id 供总部预绑定项目关联。
- `GET /api/business/tasks/<id>`：状态及原工作台 job/Channel 事件、结果来源、证据与原因。
- `POST /api/business/tasks/<id>/cancel`：只取消尚未执行的 queued；执行中按原结果核对，不假称已取消平台动作。
- `GET /api/business/events?after=<cursor>`：`{items,cursor}`，持久递增，每项含 `kind,payload,task_id,goal_id,project_id,action_id,account_id`。新消息只关联已授权且已在任务中绑定的具体会话，不猜测无归属消息。消息缓存需由现有同步/监听实际更新；接口不会开启关闭的监听。

状态：queued/running/waiting_model/waiting_platform/succeeded/failed/uncertain/cancelled。任务成功是该业务操作成功，不等同整个目标/成交成功。

能力和参数：

| 操作 | input | 结果 |
|---|---|---|
| product.read | item_id 可选 | 商品缓存，可选授权商品范围 |
| conversation.read | 空 | 会话缓存，可选授权会话范围 |
| message.read | conversation_id | 指定授权会话消息缓存 |
| conversation.sync | 空 | 原平台最近会话同步 job |
| message.history | conversation_id, older 可选 | 原平台历史同步 job |
| draft.create | description,price,media_ids,location_index,delivery 等 | 本地草稿，不发布 |
| product.publish | draft_id | 已存在且资料完整的授权草稿发布 job |
| dm.reply | conversation_id,text | 复用手动文字发送；正文必须已经核实 |
| dm.initiate | peer_id,item_id | 只创建商品咨询会话，不自动发送消息；实际商品卖家必须匹配 peer_id |
| comment.reply | item_id,comment_id,text | 必须有该账号工作台已验证的评论记录 |
| reply.draft | conversation_id,text 可选 | 原 workbench→Channel→Multica 草稿，不发送 |
| content.analyze | item_id 或 draft_description，text 可选 | 原 Channel 内容分析，不发布 |

`public.search`、`product.modify` 明确禁用，不能以本地备注/标签修改代替真实商品修改。评论能力依据真实平台适配标记；模型能力依据账号绑定，执行时再验证 agent/route 未变化。

会话读取/回复/草稿必须命中 `conversation_ids`；发布、创建商品咨询会话、评论必须命中 `resource_ids`（发布为草稿 ID）。提交及排队 job 真正执行前均重查账号、版本、开关、期限、操作和具体目标范围；发送/发布在平台副作用前额外检查。未知写操作只查询原 job，不盲目重发。崩溃后只恢复尚未开始的 queued；已开始而原 job 不存在的任务标 uncertain。

模型返回来源 `channel_model_draft` 且 `platform_action_performed=false`。演示平台结果标 `demo_platform_job`，缓存来源标 `authenticated_workbench_cache`。这些都不是成交证据。

测试只使用临时目录和 DemoPlatform，无真实发帖/发消息。运行入口启动业务调度线程；通过 create_app 注入的测试需显式调用 business.tick 或 business.start。现有管理员是 authority 唯一可信 principal，不支持任意前端消息自报身份。

授权 project_id 必须匹配每张业务任务。include_goal_created 仅引用该目标、该账号、服务器记录来源任务的新建草稿/商品/会话，不包含任意已有资源；capabilities 同时列出已有草稿供明确选择。dm.initiate 还需 peer_id 命中 contact_ids 且实际商品卖家匹配。新建会话来源在同目标/账号持久记账后才允许后续已授权回复操作。任务状态和 outbox 事件在同一 SQLite 事务提交，包含首个 queued 事件；故障回滚不留下无事件状态。

撤回 contact_ids 中的联系人后，仅通过目标新建范围继承的会话不再允许回复；明确单独授权的已有 conversation_ids 仍按该会话授权判定。

每项 grant 可带独立 expires_at：服务器按当前操作匹配的具体账号授权判断到期，不让一项到期阻断其他仍有效账号。缺省字段沿用共同短租约；共同租约仍需续期。原保存 grant 列表及期限同 revision 保持不变，租约续期只改顶层 expires_at。

Channel 模型终态兼容真实 succeeded 和演示 completed；needs_context/blocked/error、无有效最终回复/内容均转 failed。结构化 JSON 的序列化 text 不作为缺失正文的替代证据。
