"""测试前自检：确认本地环境、权限边界、审计库和 MCP 依赖都就绪。"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Config, Layout, default_layout, load_config, workspace_path
from .connectors import resolve_connector
from .tool_meta import TOOL_POLICY


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _workspace_writable(workspace: Path) -> bool:
    return workspace.is_dir() and os.access(workspace, os.W_OK | os.X_OK)


def run_doctor(config: Config | None = None, layout: Layout | None = None) -> dict[str, Any]:
    layout = layout or default_layout()
    try:
        cfg = config or load_config(layout)
    except Exception as exc:  # 配置错误也应作为一条检查失败，而不是直接崩溃
        return {
            "ok": False,
            "checks": [asdict(Check("配置", False, f"读取失败：{type(exc).__name__}: {exc}"))],
            "next_steps": ["运行 freeagent setup 或修复 ~/.freeagent/config.json"],
        }

    checks: list[Check] = []
    checks.append(Check(
        "Python 版本",
        sys.version_info >= (3, 11),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}（需 >=3.11）",
    ))
    checks.append(Check(
        "配置文件", layout.config_path.exists(), str(layout.config_path)
    ))
    workspace = workspace_path(cfg)
    checks.append(Check("工作区存在", workspace.is_dir(), str(workspace)))
    checks.append(Check("工作区可写", _workspace_writable(workspace), "需要写权限才能执行授权的写入测试"))

    if layout.home.exists():
        mode = stat.S_IMODE(layout.home.stat().st_mode)
        checks.append(Check("本地目录权限", mode & 0o077 == 0, f"{layout.home} mode={oct(mode)}（应为 0700）"))
    else:
        checks.append(Check("本地目录权限", False, f"{layout.home} 不存在，请先 setup"))

    try:
        audit = AuditLog(layout)
        task_count = len(audit.list_tasks(1000))
        checks.append(Check("审计数据库", True, f"{layout.db_path}，已有 {task_count} 条任务"))
    except Exception as exc:
        checks.append(Check("审计数据库", False, f"无法打开：{type(exc).__name__}: {exc}"))

    checks.append(Check(
        "MCP 依赖", importlib.util.find_spec("mcp") is not None,
        "已安装 mcp SDK" if importlib.util.find_spec("mcp") else "运行 pip install -e '.[mcp]'",
    ))

    manual = resolve_connector("manual").health()
    web = resolve_connector("web").health()
    checks.append(Check("手动连接器", manual.available, manual.detail))
    checks.append(Check("网页自动化默认禁用", not web.available, web.status))

    connectors: list[dict[str, Any]] = []
    for connector_id in ("manual", "mock", "web"):
        connector = resolve_connector(connector_id)
        health = connector.health()
        connectors.append({
            "id": connector.id,
            "label": connector.label,
            "available": health.available,
            "status": health.status,
            "detail": health.detail,
        })

    deny_total = len(set(cfg.deny_names)) + len(set(cfg.deny_suffixes))
    rules_total = sum(len(v.get("deny_names", [])) + len(v.get("deny_suffixes", [])) for v in cfg.workspace_rules.values())
    checks.append(Check("路径拒绝规则", True, f"全局 {deny_total} 条，工作区额外 {rules_total} 条"))

    ok = all(item.ok for item in checks)
    next_steps: list[str] = []
    if not layout.config_path.exists():
        next_steps.append("freeagent setup")
    if not ok:
        next_steps.append("修复上面的失败项后重新运行 freeagent doctor")
    else:
        next_steps.extend([
            "openclaw mcp doctor freeagent --probe",
            "openclaw mcp reload",
            "按 docs/OPENCLAW-SETUP-FOR-CLAUDE.md 跑第一波只读任务",
        ])

    return {
        "ok": ok,
        "checks": [asdict(item) for item in checks],
        "connectors": connectors,
        "workspace": str(workspace),
        "audit_retention_days": cfg.audit_retention_days,
        "audit_auto_purge": cfg.audit_auto_purge,
        "tool_count": len(TOOL_POLICY),
        "write_enabled": os.environ.get("FREEAGENT_MCP_ALLOW_WRITE", "0").strip().lower() in {"1", "true", "yes", "on"},
        "reads_enabled": os.environ.get("FREEAGENT_MCP_ALLOW_READS", "0").strip().lower() in {"1", "true", "yes", "on"},
        "next_steps": next_steps,
    }
