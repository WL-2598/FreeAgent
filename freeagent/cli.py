"""FreeAgent 命令行入口。"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer

from . import config as config_module
from .audit import AuditLog
from .config import Config, Layout, load_config, save_config, setup
from .connectors import ManualConnector, MockConnector, resolve_connector
from .errors import ConnectorError, FreeAgentError
from .orchestrator import AutoApproveUI, ConsoleUI, TaskRunner, cancel_task
from .utils import short_id

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="FreeAgent：网页模型规划 + 本地受限工具执行（默认 dry-run，修改需确认）",
)
config_app = typer.Typer(help="查看和修改本地配置")
app.add_typer(config_app, name="config")

COMMANDS = {
    "setup", "models", "login", "run", "approve", "reject", "status",
    "cancel", "undo", "history", "purge", "demo", "task-demo", "mcp", "config", "--help", "-h",
}


def _layout_and_config() -> tuple[Layout, Config]:
    layout = Layout()
    if not layout.config_path.exists():
        raise FreeAgentError("尚未初始化。请先运行：freeagent setup")
    return layout, load_config(layout)


def _runner(layout: Layout, cfg: Config, ui: Optional[object] = None) -> TaskRunner:
    return TaskRunner(cfg, layout=layout, ui=ui)  # type: ignore[arg-type]


def _print_result(task: dict) -> None:
    status = task.get("status")
    typer.echo(f"\n状态：{status} · 任务 ID：{task.get('id')}")
    if task.get("summary"):
        typer.echo(f"总结：{task['summary']}")
    if task.get("error"):
        typer.echo(f"错误：{task['error']}")
    if status == "awaiting_approval":
        typer.echo(f"下一步：freeagent approve {task['id']}  或  freeagent reject {task['id']}")
    elif status == "done":
        typer.echo(f"可查看：freeagent status {task['id']} · 可撤销：freeagent undo {task['id']}")
    typer.echo("审计：freeagent history；连接器使用条款与免费额度由第三方决定，本产品不作保证。")


def _exit_for(task: dict) -> None:
    status = task.get("status")
    if status == "done":
        raise typer.Exit(0)
    if status == "awaiting_approval":
        raise typer.Exit(2)
    raise typer.Exit(3)


@app.command()
def setup() -> None:
    """创建工作区与本地状态目录，显示隐私与免费服务说明。"""
    cfg, path = config_module.setup()
    typer.echo("FreeAgent 初始化完成。")
    typer.echo(f"本地配置：{path}（权限 0600）")
    typer.echo(f"工作区：{Path(cfg.workspace).expanduser()}（默认只允许读写此目录）")
    typer.echo("\n重要说明：")
    typer.echo("  · 默认使用手动粘贴模式：你自己在浏览器登录，程序不索取/保存密码或 Cookie。")
    typer.echo("  · 模型回复是不可信输入；所有动作都经本地校验，写入/读取/打开网页需确认。")
    typer.echo("  · 免费额度、地区可用性、模型版本和网站规则由第三方决定，本产品不作保证。")
    typer.echo("  · 网页自动化连接器在确认目标网站条款允许前保持禁用，不提供任何绕过方案。")
    typer.echo("\n下一步：freeagent models；然后 freeagent \"列出工作区文件并给出建议\"")
    models()


@app.command()
def models() -> None:
    """列出连接器与健康状态（不对第三方服务可用性作保证）。"""
    names = ["manual", "mock", "web"]
    for name in names:
        connector = resolve_connector(name)
        health = connector.health()
        flag = "可用" if health.available else "不可用"
        typer.echo(f"[{flag}] {connector.label} (id={name})")
        typer.echo(f"        {health.detail}")
    typer.echo("\n说明：即使连接器显示可用，也不代表第三方网站当前可访问或有免费额度。")


@app.command()
def login(connector: str = typer.Argument("manual", help="连接器 ID")) -> None:
    """打开/说明登录流程；永远不会索取账号密码。"""
    item = resolve_connector(connector)
    typer.echo(f"{item.label}：{item.login()}")


@app.command()
def run(
    task: str = typer.Argument(..., help="自然语言任务描述"),
    connector: str = typer.Option("manual", "--connector", "-c", help="连接器 ID"),
    response_file: Optional[Path] = typer.Option(
        None, "--response-file", exists=True, dir_okay=True, file_okay=True,
        help="手动模式的回复文件/目录（用于脚本或非交互环境）",
    ),
    task_id: Optional[str] = typer.Option(None, "--task-id", help="自定义任务 ID"),
) -> None:
    """创建并执行任务（默认逐项确认；非交互终端会暂停等待 approve）。"""
    layout, cfg = _layout_and_config()
    item = resolve_connector(connector, responses=response_file)
    task_record = _runner(layout, cfg).run(task, item, task_id=task_id)
    _print_result(task_record)
    _exit_for(task_record)


@app.command()
def approve(
    task_id: str = typer.Argument(..., help="处于 awaiting_approval 的任务 ID"),
    connector: str = typer.Option("manual", "--connector", "-c", help="后续轮次使用的连接器"),
    response_file: Optional[Path] = typer.Option(None, "--response-file", exists=True),
) -> None:
    """批准已预览的动作，并继续执行任务。"""
    layout, cfg = _layout_and_config()
    item = resolve_connector(connector, responses=response_file)
    task_record = _runner(layout, cfg).resume(task_id, True, item)
    _print_result(task_record)
    _exit_for(task_record)


@app.command()
def reject(task_id: str = typer.Argument(..., help="任务 ID")) -> None:
    """拒绝已预览的动作并取消任务。"""
    layout, cfg = _layout_and_config()
    task_record = _runner(layout, cfg).resume(task_id, False, resolve_connector("manual"))
    _print_result(task_record)
    _exit_for(task_record)


@app.command()
def status(task_id: str = typer.Argument(..., help="任务 ID")) -> None:
    """查看任务状态、动作历史与撤销记录。"""
    layout, cfg = _layout_and_config()
    audit = AuditLog(layout)
    task_record = _runner(layout, cfg, ui=ConsoleUI()).status(task_id)
    typer.echo(f"任务：{task_record['id']} · 状态：{task_record['status']}")
    typer.echo(f"目标：{task_record['goal']}")
    typer.echo(f"连接器：{task_record['connector']} · 工作区：{task_record['workspace']}")
    typer.echo(f"步骤：{task_record['step']} · 模型轮次：{task_record['rounds']}")
    if task_record.get("summary"):
        typer.echo(f"总结：{task_record['summary']}")
    if task_record.get("error"):
        typer.echo(f"错误：{task_record['error']}")
    typer.echo("\n动作记录：")
    for action in audit.list_actions(task_id):
        typer.echo(f"  #{action['seq']} [{action['decision']}] {action['action']} -> {action['result']}")
        if action.get("approver") or action.get("preview_hash"):
            digest = (action.get("preview_hash") or "")[:12]
            typer.echo(f"      批准人：{action.get('approver') or 'unspecified'} · 预览哈希：{digest}…")
    pending = task_record.get("pending_preview")
    if pending:
        typer.echo(f"\n待批准预览：\n{pending}")
    undoable = audit.undos(task_id)
    typer.echo(f"\n可撤销记录：{len(undoable)} 条")


@app.command()
def cancel(task_id: str = typer.Argument(..., help="任务 ID")) -> None:
    """请求中止任务；运行中的任务在下一步开始前安全停止。"""
    layout = Layout()
    cancel_task(task_id, layout)
    typer.echo(f"已请求取消任务 {task_id}。运行中的任务不会在当前动作中途被强杀。")


@app.command()
def undo(task_id: str = typer.Argument(..., help="任务 ID")) -> None:
    """按逆序撤销该任务支持撤销的移动/写入/新建目录。"""
    layout, cfg = _layout_and_config()
    _runner(layout, cfg).undo(task_id)


@app.command()
def history(limit: int = typer.Option(20, "--limit", "-n", min=1, max=200)) -> None:
    """查看最近的本地任务审计记录。"""
    layout = Layout()
    rows = AuditLog(layout).list_tasks(limit)
    if not rows:
        typer.echo("暂无任务记录。")
        return
    for row in rows:
        typer.echo(
            f"{row['created_at']}  [{row['status']:<16}] {row['id']}  "
            f"{row['goal'][:70]}{'…' if len(row['goal']) > 70 else ''}"
        )


@app.command()
def mcp() -> None:
    """以 stdio MCP 服务器启动，供 OpenClaw 等客户端接入（不要手动在终端直接运行）。"""
    from .mcp_server import main as mcp_main

    mcp_main()


@app.command()
def purge(
    days: Optional[int] = typer.Option(None, "--days", min=1, help="保留天数，默认取配置 audit_retention_days"),
) -> None:
    """清理超过保留期的本地任务与审计记录（仅本机）。"""
    layout, cfg = _layout_and_config()
    keep = days or cfg.audit_retention_days
    removed = AuditLog(layout).purge_old(keep)
    typer.echo(f"已清理 {removed} 个早于 {keep} 天的任务及其动作、撤销与审计记录。")


@config_app.command("show")
def config_show() -> None:
    """显示当前配置（不会显示任何凭据）。"""
    _, cfg = _layout_and_config()
    typer.echo(cfg.model_dump_json(indent=2, exclude_none=True))


@config_app.command("allow-dir")
def config_allow_dir(path: str = typer.Argument(..., help="要授权为只读的额外目录")) -> None:
    """显式授权一个额外只读目录；写入仍仅限主工作区。"""
    layout, cfg = _layout_and_config()
    resolved = str(Path(path).expanduser().resolve())
    if resolved not in cfg.extra_roots:
        cfg.extra_roots.append(resolved)
        save_config(cfg, layout)
    typer.echo(f"已授权只读目录：{resolved}")


@config_app.command("deny-name")
def config_deny_name(
    value: str = typer.Argument(..., help="要拒绝的文件名，例如 secrets.txt"),
    workspace: bool = typer.Option(False, "--workspace", help="只作用于当前工作区"),
) -> None:
    """追加敏感文件名规则（默认规则不可移除，只能更严格）。"""
    layout, cfg = _layout_and_config()
    name = value.strip().lower()
    if not name or "/" in name:
        raise FreeAgentError("文件名格式无效")
    if workspace:
        key = str(Path(cfg.workspace).expanduser().resolve())
        rules = cfg.workspace_rules.setdefault(key, {})
        names = rules.setdefault("deny_names", [])
        if name not in names:
            names.append(name)
    elif name not in cfg.deny_names:
        cfg.deny_names.append(name)
    save_config(cfg, layout)
    typer.echo(f"已加入拒绝名单：{name}（{'仅当前工作区' if workspace else '全局'}）")


@config_app.command("deny-suffix")
def config_deny_suffix(
    value: str = typer.Argument(..., help="要拒绝的后缀，例如 .bak"),
    workspace: bool = typer.Option(False, "--workspace", help="只作用于当前工作区"),
) -> None:
    """追加敏感后缀规则（默认规则不可移除，只能更严格）。"""
    layout, cfg = _layout_and_config()
    suffix = value.strip().lower()
    suffix = suffix if suffix.startswith(".") else "." + suffix
    if len(suffix) < 2 or "/" in suffix:
        raise FreeAgentError("后缀格式无效")
    if workspace:
        key = str(Path(cfg.workspace).expanduser().resolve())
        rules = cfg.workspace_rules.setdefault(key, {})
        suffixes = rules.setdefault("deny_suffixes", [])
        if suffix not in suffixes:
            suffixes.append(suffix)
    elif suffix not in cfg.deny_suffixes:
        cfg.deny_suffixes.append(suffix)
    save_config(cfg, layout)
    typer.echo(f"已加入拒绝后缀：{suffix}（{'仅当前工作区' if workspace else '全局'}）")


@config_app.command("allow-domain")
def config_allow_domain(domain: str = typer.Argument(..., help="例如 example.com")) -> None:
    """把域名加入 browser.open 白名单。"""
    layout, cfg = _layout_and_config()
    value = domain.strip().lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9.-]{2,253}", value) or ".." in value:
        raise FreeAgentError("域名格式无效")
    if value not in cfg.allowed_domains:
        cfg.allowed_domains.append(value)
        save_config(cfg, layout)
    typer.echo(f"已允许域名：{value}")


@app.command()
def demo() -> None:
    """用模拟模型和临时工作区演示「列文件 → 归档 → 摘要」闭环（不联网）。"""
    temp_root = Path(tempfile.mkdtemp(prefix="freeagent-demo-"))
    workspace = temp_root / "workspace"
    docs = workspace / "docs"
    docs.mkdir(parents=True)
    for name in ("report-2023.pdf", "report-2024.pdf", "notes.txt"):
        (docs / name).write_bytes(b"%PDF-1.4 demo\n" if name.endswith(".pdf") else b"demo notes\n")
    layout = Layout(temp_root / "state")
    cfg = Config(
        workspace=str(workspace),
        allowed_domains=["example.com"],
        max_steps=10,
        max_rounds=15,
    )
    save_config(cfg, layout)
    replies = [
        '{"action":"fs.list","arguments":{"path":"."},"reason":"先查看工作区结构"}',
        '{"action":"fs.list","arguments":{"path":"docs"},"reason":"列出待归档的 PDF"}',
        '{"action":"fs.mkdir","arguments":{"path":"archive"},"reason":"建立归档目录"}',
        '{"action":"fs.move","arguments":{"source":"docs/report-2023.pdf","destination":"archive/report-2023.pdf"},"reason":"按年份归档"}',
        '{"action":"fs.write_text","arguments":{"path":"archive/summary.md","content":"# 归档摘要\\n- report-2023.pdf 已归档\\n","overwrite":false},"reason":"生成摘要"}',
        '{"action":"task.done","arguments":{"summary":"已列出 PDF、创建归档目录、移动文件并生成摘要。"},"reason":"目标完成"}',
    ]
    connector = MockConnector(replies)
    typer.echo("演示模式：模拟模型 + 临时工作区 + 自动批准（仅用于演示，不代表真实网页模型）。")
    typer.echo(f"临时工作区：{workspace}")
    runner = TaskRunner(cfg, layout=layout, ui=AutoApproveUI())
    task_record = runner.run("列出工作区中的 PDF，提出归档方案，移动 2023 年报告并写摘要。", connector, task_id=short_id())
    typer.echo(f"\n演示结束：{task_record['status']}。演示目录保留在：{temp_root}")
    _exit_for(task_record)


def _demo_action(name: str, **arguments: object) -> str:
    return json.dumps({"action": name, "arguments": arguments, "reason": "演示"}, ensure_ascii=False)


@app.command("task-demo")
def task_demo() -> None:
    """演示任务级 MCP 闭环（模拟模型回复 + 临时工作区，不联网）。"""
    root = Path(tempfile.mkdtemp(prefix="freeagent-task-demo-"))
    workspace = root / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "report-2024.pdf").write_bytes(b"%PDF-1.4 demo\n")
    layout = Layout(root / "state")
    cfg = Config(workspace=str(workspace), max_steps=10, max_rounds=15)
    save_config(cfg, layout)

    from .audit import AuditLog
    from .task_session import TaskSessions

    sessions = TaskSessions(cfg, AuditLog(layout), allow_write=True)
    started = sessions.start("列出工作区文件，并写一份简短摘要")
    task_id = started["task_id"]
    typer.echo(f"任务级 MCP 演示（模拟模型 + 临时工作区）：{workspace}")
    typer.echo(f"任务 ID：{task_id}；状态：{started['status']}")
    typer.echo("fa_task_start 返回的提示词包含安全规则、工具白名单与本地边界。")

    replies = [
        _demo_action("fs.list", path="."),
        _demo_action("fs.write_text", path="summary.md", content="# 摘要\n发现 docs/report-2024.pdf\n"),
        _demo_action("task.done", summary="已列出文件并写入摘要"),
    ]
    for reply in replies:
        result = sessions.reply(task_id, reply)
        typer.echo(f"\nfa_task_reply → {result['status']}：{result.get('action', '终止动作')}")
        if result["status"] == "awaiting_approval":
            typer.echo("预览（此时文件未发生任何变化）：")
            typer.echo(result["preview"])
            approved = sessions.approve(task_id, result["approval_token"])
            typer.echo(f"fa_task_approve → {approved['status']}：{approved['result']}")

    status = sessions.status(task_id)
    typer.echo(f"\n最终状态：{status['status']}；审计动作 {len(status['actions'])} 条；可撤销 {status['undoable']} 条")
    typer.echo(f"fa_task_undo → {sessions.undo(task_id)['results'][0]}")
    typer.echo(f"演示目录保留在：{root}")


def main() -> None:
    """支持 `freeagent "任务描述"` 直接创建任务。"""
    args = sys.argv[1:]
    if args and args[0] not in COMMANDS and not args[0].startswith("-"):
        sys.argv.insert(1, "run")
    try:
        app()
    except FreeAgentError as exc:
        typer.echo(f"错误：{exc}", err=True)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        typer.echo("\n已安全中止。", err=True)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
