# FreeAgent

本地终端智能体：**网页模型负责理解/规划，本地程序负责校验/执行**。模型输出永远是不可信输入。

MVP 现状（v0.1.0）：

- Linux / Python 3.11+，从终端启动；无付费 API Key、不联网调用模型 API。
- 默认 **手动粘贴模式**：程序打印提示词，你在自己已登录的浏览器里获取回复并粘贴回来；不索取/保存密码或 Cookie。
- 单次只执行一个结构化动作；严格 JSON Schema（Pydantic）+ 工具白名单 + 工作区沙箱 + 域名白名单。
- 默认 dry-run：只读动作可直接执行；读取文本、打开网页和所有变更动作都需明确确认；非交互终端会暂停等待 `approve`。
- 本地 SQLite/JSONL 审计、逐项 undo、步数/往返/大小/超时上限、Ctrl-C 与外部 `cancel` 安全停机。
- 网页自动化连接器**默认禁用**：必须先逐站确认服务条款允许，不绕过验证码、付费墙、速率限制或登录态。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # 依赖：pydantic、typer；均为开源免费许可
freeagent setup             # 创建 ~/.freeagent 与 ~/agent-workspace
freeagent demo              # 不联网的端到端演示（模拟模型 + 临时工作区）
```

开发/测试：`pip install -e '.[dev]' && pytest -q`

## 快速使用

```bash
freeagent models                     # 查看连接器状态（不代表第三方服务可用）
freeagent "列出工作区中的 PDF，提出按年份归档方案"   # 等价于 freeagent run "..."
# 程序展示提示词 → 复制到你已登录的网页模型 → 粘贴回复（单独一行 END 结束）
# 对写入/读取/打开网页逐一预览并确认；或暂停后：
freeagent status <task-id>
freeagent approve <task-id>          # 或 freeagent reject <task-id>
freeagent undo <task-id>
freeagent history
freeagent config allow-dir /path/to/extra   # 显式授权额外只读目录
freeagent config allow-domain example.org   # 域名白名单
```

非交互脚本可用 `--response-file 目录/文件` 提供每轮模型回复（文件名 `01.txt`、`02.txt` …）；
仍不会跳过确认：未确认的动作会暂停为 `awaiting_approval`。

## 接入 OpenClaw

FreeAgent 提供 stdio MCP 服务器（`pip install -e '.[mcp]'`），OpenClaw 可直接挂载白名单工具。零散变更用 `fa_prepare` → 预览/批准 → `fa_commit` 两阶段；完整任务用 `fa_task_start` → `fa_task_reply`（提交网页模型回复）→ `fa_task_approve`（用户确认）→ 继续 reply，直到完成，并可 `fa_task_status` / `fa_task_list` / `fa_task_undo`；另有 `fa_find` 可按文件名安全定位文件。默认只读，令牌一次性且绑定动作参数（任务级令牌只存哈希，重启后仍可在有效期内批准）；状态保存在本地 SQLite。完整注册命令、工具表、安全配置与测试流程见 [docs/OPENCLAW.md](docs/OPENCLAW.md)；可直接发给另一个智能体的配置说明见 [docs/OPENCLAW-SETUP-FOR-CLAUDE.md](docs/OPENCLAW-SETUP-FOR-CLAUDE.md)。

## 动作协议（节选）

模型每轮只能输出一个对象，例如：

```json
{"action": "fs.move", "arguments": {"source": "report.pdf", "destination": "2025/report.pdf"}, "reason": "按年份归档"}
```

白名单：`fs.list`、`fs.read_text`、`fs.mkdir`、`fs.move`、`fs.write_text`、`browser.open`，
终止动作 `task.done` / `task.blocked`。未知动作、未知字段、多个对象、shell 命令字符串一律拒绝。
详见 [docs/PRD.md](docs/PRD.md) 与 [docs/SECURITY.md](docs/SECURITY.md)。

## 目录结构

```text
freeagent/cli.py            CLI 与交互确认
freeagent/orchestrator.py   状态机、单步闭环、取消/超时/暂停恢复
freeagent/protocol.py       结构化动作 schema 与严格解析
freeagent/policy.py         路径沙箱、敏感文件过滤、域名白名单
freeagent/tools/            本地白名单工具与撤销
freeagent/connectors/       manual（默认）、mock（测试）、web（禁用占位）
freeagent/planner.py        提示模板与不可信数据包装
freeagent/audit.py          本地审计与撤销记录
tests/                      安全与流程自动化测试
```

## 边界和免责声明

免费额度、地区可用性、模型版本、服务条款和界面都由第三方决定；本产品**不保证永久免费、无限量或始终提供最强模型**，
不收取模型 API 费用，也不把请求发往开发者自有服务器。网页自动化是否被允许须逐站验证；不提供任何规避手段。
用户自行承担设备、网络与账号使用责任。详细风险与隐私说明见 [docs/SECURITY.md](docs/SECURITY.md)、
[docs/CONNECTORS.md](docs/CONNECTORS.md)。

许可证：MIT。
