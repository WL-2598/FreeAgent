# FreeAgent

本地终端智能体：**网页模型负责理解/规划，本地程序负责校验/执行**。模型输出永远是不可信输入。

MVP 现状（v0.1.0）：

- Linux / Python 3.11+，从终端启动；无付费 API Key、不联网调用模型 API。
- 默认 **手动粘贴模式**：程序打印提示词，你在自己已登录的浏览器里获取回复并粘贴回来；不索取/保存密码或 Cookie。
- 单次只执行一个结构化动作；严格 JSON Schema（Pydantic）+ 工具白名单（含安全的 `fs.find` 文件名定位）+ 工作区沙箱 + 域名白名单。
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

可选依赖分组：`.` 只需 `pydantic` + `typer`；`[dev]` 增加 `pytest`、`mcp`（自检用）；`[mcp]` 只为 OpenClaw stdio 接入增加 `mcp`；`[web]` 声明 Playwright，但在网站条款验证前**不会启用任何网页自动化**。

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
freeagent purge                  # 预览过期审计清理（默认不删）
freeagent purge --apply          # 确认后才真正清理
freeagent doctor --json          # 机器可读自检，供 Claude/OpenClaw 解析
```

审计自动清理默认关闭；需要时用 `freeagent config auto-purge true` 显式开启（`false` 关闭），否则任何启动流程都不会删除历史记录。

非交互脚本可用 `--response-file 目录/文件` 提供每轮模型回复（文件名 `01.txt`、`02.txt` …）；
仍不会跳过确认：未确认的动作会暂停为 `awaiting_approval`。

## 接入 OpenClaw：打开它就能自动接管网页模型

```bash
freeagent openclaw install-skill     # 安装 /freeagent-web-loop 技能到 OpenClaw 工作区
openclaw skills list | grep freeagent-web-loop
```

之后你只需要在 OpenClaw 里说：

```text
/freeagent-web-loop 把 2023 年的 PDF 移动到 archive/，并写一份摘要
```

OpenClaw 会自己打开/聚焦你已登录的网页模型窗口、输入 FreeAgent 给的提示词、等生成结束、
把回复原文交回 FreeAgent，循环到结束；遇到验证码/登录失效/限流立即停手回报你。
**任何文件变更都会先给你看预览、等你批准**，FreeAgent 不访问网页、不读 Cookie、不绕限制。
也可以先生成一段简报再粘贴：`freeagent openclaw brief "<任务>"`。

FreeAgent 提供 stdio MCP 服务器（`pip install -e '.[mcp]'`），OpenClaw 可直接挂载白名单工具。零散变更用 `fa_prepare` → 预览/批准 → `fa_commit` 两阶段；完整任务用 `fa_task_start` → `fa_task_reply`（提交网页模型回复）→ `fa_task_approve`（用户确认）→ 继续 reply，直到完成，并可 `fa_task_status` / `fa_task_list` / `fa_task_undo`；另有 `fa_find` 可按文件名安全定位文件；`fa_task_start` 返回的 `driver` 字段直接告诉 OpenClaw 用哪个浏览器 profile、怎么循环、什么情况下必须停手，`fa_task_reissue` 可在令牌丢失/过期时重签（仍是预览，仍需你同意）。默认只读，令牌一次性且绑定动作参数（任务级令牌只存哈希，重启后仍可在有效期内批准）；批准人与「所见预览哈希」写入审计。所有 MCP 工具带 `readOnlyHint` / `destructiveHint` / `openWorldHint` 元数据，便于 OpenClaw 过滤。敏感文件名/后缀可通过 `freeagent config deny-name` / `deny-suffix` 追加（默认规则不可移除，可 `--workspace` 限定工作区）；提示词每次附带最近动作摘要和「不要重复无新信息的动作」规则，且本地会确定性拦截完全重复的上一动作；`fa_task_status` 在等待模型时会返回 `next_prompt`，服务重启后可续跑。撤销会恢复原内容与时间戳。可用 `freeagent purge` 清理超过保留期的本地审计。拉取后先运行 `freeagent doctor`（或通过 MCP 调 `fa_doctor`）确认环境就绪。完整注册命令、工具表、安全配置与测试流程见 [docs/OPENCLAW.md](docs/OPENCLAW.md)；可直接发给另一个智能体的配置说明见 [docs/OPENCLAW-SETUP-FOR-CLAUDE.md](docs/OPENCLAW-SETUP-FOR-CLAUDE.md)。

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

## 为什么走免费网页模型，而不是买 API / 额度

FreeAgent 的设计目标是**零模型费用、零账号凭据托管**：提示词由程序打印，你复制到自己已登录的网页模型，再把回复粘贴回来。这样无需 API Key、不会产生模型账单，也不会把文件上下文发到开发者自有服务器。

代价是这条路径受第三方网页服务约束：免费额度、地区可用性、界面改版、验证码、速率限制和服务条款都由网站决定，且需要人工搬运文本；它**不适合无人值守或需要稳定 SLA 的场景**。付费 API 的优势是稳定的程序化接口和明确的配额，但那意味着账单与供应商条款，本项目不默认采购、也不承诺任何网站可自动化。

如果你后续确实需要稳定自动化，可以在合规前提下自行提供模型访问能力；当前 MVP 仍把手动粘贴作为唯一可信路径，并且无论模型从哪来，本地策略、逐项确认和审计边界都不变。

## 边界和免责声明

免费额度、地区可用性、模型版本、服务条款和界面都由第三方决定；本产品**不保证永久免费、无限量或始终提供最强模型**，
不收取模型 API 费用，也不把请求发往开发者自有服务器。网页自动化是否被允许须逐站验证；不提供任何规避手段。
用户自行承担设备、网络与账号使用责任。详细风险与隐私说明见 [docs/SECURITY.md](docs/SECURITY.md)、
[docs/CONNECTORS.md](docs/CONNECTORS.md)。

许可证：MIT。
