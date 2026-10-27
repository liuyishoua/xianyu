# xianyu

[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE)

独立运行的闲鱼工作台，基于 [cv-cat/XianYuApis](https://github.com/cv-cat/XianYuApis) 增加 Web 控制台。
包含账号矩阵、持续私信监听、Multica 助手路由、会话历史、文字和图片消息、本地草稿、图片上传与商品发布。
界面参考项目内 XHS 工作台，提供独立的账号、监听、发布、模型与任务页面。
Python 后端、原生 HTML/JS 前端、SQLite 数据库，无需 Bifrost 或前端编译。

## 界面预览

以下为本项目离线演示模式截图，账号和内容均为模拟数据。

![闲鱼工作台演示](docs/images/overview.png)

![闲鱼账号矩阵演示](docs/images/account-matrix.png)

## 一键启动

先安装 **Python 3.10+**，克隆仓库后运行：

```bash
git clone https://github.com/liuyishoua/xianyu.git
cd xianyu
python3 start.py
```

Windows 可双击 `start.bat`；macOS 可双击 `start.command`；Linux 可运行 `./start.sh`。
首次启动自动创建 `.venv`、安装 Python 依赖，成功后尝试打开 **http://127.0.0.1:8770**。
首次安装需要联网，后续依赖不变时直接启动。按 Ctrl+C 停止。

默认是**演示模式**，使用独立模拟账号和内容，不联系真实平台。
工作台访问密码保存在 `.local/web-demo/admin-password`；工作台登录与平台账号连接分别管理。

```bash
# 更换端口或在服务器上运行
python3 start.py --port 8870 --no-browser
```

## 真实账号模式

安装 **Node.js 20+**（平台签名辅助代码需要时使用），然后运行：

```bash
python3 start.py --real
```

支持扫码连接。消息发送和商品发布需要用户操作；接口目前不提供商品下架、删除、退款或订单管理。
首次运行的访问密码保存在 `.local/web/admin-password`，进入后连接自己的平台账号。
真实能力受登录状态、账号权限和平台接口返回影响；演示运行成功不代表真实平台业务已经联调。
模型起草与分析由可选 Channel 接入提供，普通查询无需部署 Channel。
商品留言已接入官方移动端读取接口，按账号轮询已导入的自有商品，首次补收近 24 小时留言，并可交给 Multica 起草。真实留言回复接口仍未接通，草稿需复制到闲鱼 App 发送；自动发送已禁用。

## 数据与备份

- 演示数据：`.local/web-demo/`；真实工作台数据：`.local/web/`。
- 账号会话、Cookie、访问密码、数据库和上传素材只保存在本地。
- 多账号会话位于 `.local/web/accounts/`，已有会话会自动迁移；其他账号监听不受当前账号切换影响。
- 收藏、标签、备注和草稿按平台账号隔离，保存草稿不会自动发布。
- 真实写入使用请求 ID 去重；结果不确定时不自动重试。
- 备份前停止服务，复制整个 `.local/`；数据、`.env`、密钥和虚拟环境不提交 Git。

## 手动安装与验证

```bash
python3 -m venv .venv
# Linux / macOS；Windows 使用 .venv\Scripts\python.exe
.venv/bin/python -m pip install -r requirements-web.txt
.venv/bin/python -m web --demo
.venv/bin/python -m unittest discover -s web/tests -v
node --check web/static/app.js
```

真实模式如存在根目录 `package.json`，还需安装 Node.js 依赖；一键启动会在依赖目录缺失时执行 `npm install`。
不要运行上游示例脚本作为工作台入口，它们可能输出登录态或执行真实平台操作。

## 目录

- `web/`：HTTP 服务、业务逻辑、平台适配、前端和回归测试。
- `docs/workspace.md`：功能范围、接口与部署说明。
- `docs/acceptance-v2.md`：本轮验收证据、真实平台限制与待验证项目。
- `deploy/`：systemd 和 HTTPS 反向代理示例，部署时调整域名及本机路径。
- `start.py`、`start.sh`、`start.command`、`start.bat`：一键启动入口。
- 其余平台接口、签名资源及原始例程保留原有来源标注。

原项目说明保存在 [docs/upstream-readme.md](docs/upstream-readme.md)。

## 使用许可

**本项目仅供学习、研究、测试及其他非商业用途，不得用于商业目的。**
对本仓库中版权所有者拥有权利的代码，采用
[PolyForm Noncommercial License 1.0.0](LICENSE)；商业使用须事先取得版权所有者的单独授权。
本项目属于**源码可见、限制商用**，不应标为 MIT 或 OSI 意义上的开源许可证。

第三方代码、素材和依赖仍受其各自许可条件约束，来源与许可范围见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本声明不追溯改变第三方代码或此前已发布版本的许可。


### 统一运营身份与数据归属

生产应用中心为 `https://d.58nft.net:9461/platform-select`。登录后点击工作台无需再次输入工作台密码；第三方平台子账号仍需完成对应平台授权。工作台显示当前运营用户，统一退出会撤销该用户的统一登录会话。

启用条件是数据目录内存在私有 `workspace-owner.json`，包含 `owner_id`、`owner_username` 与 `public_origin`。现有数据明确归属 Leo（用户 ID 1）；其他运营用户的数据创建在 `users/<已验证用户ID>/`，其子账号、平台登录态、任务及业务数据独立。用户身份由独立 `ops-hub` 服务（127.0.0.1:8787）逐请求验证，客户端传入的用户 ID 不参与归属判定；验证服务故障时拒绝访问。新用户不会继承 Leo 的模型、Channel 或平台凭证。

内部调用使用数据目录中的私有 `workspace-machine-key`，通过 `X-Ops-Machine-Key` 传递，明确对应原数据所有者；配置文件与密钥不得提交或输出。删除所有者配置会恢复旧模式，仅应在受控维护中进行。演示模式不启用统一身份，可使用隔离临时数据测试。Channel、总部和账号中心当前仅向已分配的原所有者开放，其他用户不会自动获得这些资源。

### 首屏资源构建

工作台首屏内嵌本地脚本与样式，避免多个静态请求阻塞启动。CSP 按实际资源内容计算 SHA256；账号与业务数据仍通过受保护接口读取，不嵌入静态页面。

修改 `web/static/app.js`、`ui.js` 或 `workspace.js` 后，在 `web/` 执行 `npm ci && npm run build`，并提交更新的 `static/app.bundle.mjs`。生产 Python 服务直接读取已构建产物，不需要 Node。
