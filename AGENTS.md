# 工作台开发约定

- 工作台位于 `web/`，入口 `python -m web`。接口在 `app.py`，存储与任务在 `service.py`，真实平台适配在 `platform.py`。
- 测试使用 `web/demo.py` 和临时数据目录；演示模式必须禁止真实平台登录和写入。
- 不运行原始示例入口验证页面，它们可能自动回复、打印凭据或发布商品。
- 不在测试中发送真实消息或发布商品，除非用户明确授权本次操作。普通 GET 和本地草稿保存不得写入闲鱼。
- 写入需保留请求去重及结果不确定时不自动重试的行为。账号缓存必须按用户 ID 隔离。
- `.local/` 保存凭据、数据库与私有测试产物，禁止提交或打印凭据。公开部署必须保留登录与同源校验。
- 修改后运行 `.venv/bin/python -m unittest discover -s web/tests -v`；前端变更运行 `node --check web/static/app.js` 并检查对应浏览器交互。
- 部署只重启 `xianyu-web.service`；门户接入遵循门户技能，不修改无关应用配置。
- Channel 可选账号连接器通过既有 HTTP 接口观察本地消息、同步最近会话和发送文字。`X-Execution-Account` 在请求期间持有账号锁，`expected_account_id` 在发送入队与执行前复核；不改变普通工作台操作。连接器默认关闭，测试仅使用 DemoPlatform，不向真实联系人发送。
