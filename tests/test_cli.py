from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from typer.testing import CliRunner

from freeagent import cli
from freeagent.audit import AuditLog
from freeagent.cli import app

from .conftest import Env, action


def invoke(*args: str):
    return CliRunner().invoke(app, list(args))


def test_setup_creates_workspace_and_explains_limits(env: Env, monkeypatch) -> None:
    monkeypatch.delenv("FREEAGENT_WORKSPACE", raising=False)
    result = invoke("setup")
    assert result.exit_code == 0, result.output
    assert "初始化完成" in result.output
    assert "免费额度" in result.output
    assert env.layout.config_path.exists()


def test_models_marks_web_automation_disabled(env: Env) -> None:
    result = invoke("models")
    assert result.exit_code == 0, result.output
    assert "web" in result.output
    assert "不可用" in result.output
    assert "手动粘贴" in result.output


def test_run_approve_and_undo_via_cli(env: Env) -> None:
    replies = env.home / "replies"
    replies.mkdir()
    (replies / "01.txt").write_text(action("fs.write_text", path="note.md", content="hello"))
    (replies / "02.txt").write_text(action("task.done", summary="完成"))

    result = invoke("run", "写一个文件", "--response-file", str(replies))
    assert result.exit_code == 2, result.output
    assert "awaiting_approval" in result.output
    assert not (env.workspace / "note.md").exists()

    task_id = [line for line in result.output.split() if "awaiting_approval" in line][-1]
    # 从输出中解析任务 ID（形如 "状态：awaiting_approval · 任务 ID：xxxx"）。
    task_id = result.output.split("任务 ID：")[-1].split()[0]

    done_file = env.home / "done.txt"
    done_file.write_text(action("task.done", summary="已批准并写入"))
    approved = invoke("approve", task_id, "--response-file", str(done_file))
    assert approved.exit_code == 0, approved.output
    assert (env.workspace / "note.md").read_text() == "hello"

    undone = invoke("undo", task_id)
    assert undone.exit_code == 0, undone.output
    assert not (env.workspace / "note.md").exists()


def test_task_demo_cli(env: Env) -> None:
    result = invoke("task-demo")
    assert result.exit_code == 0, result.output
    assert "fa_task_reply → awaiting_approval" in result.output
    assert "最终状态：done" in result.output
    assert "fa_task_undo" in result.output


def test_direct_task_dispatch(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "app", lambda: seen.append(sys.argv.copy()))
    monkeypatch.setattr(sys, "argv", ["freeagent", "列出工作区文件"])
    cli.main()
    assert seen[0][1] == "run"
    assert seen[0][2] == "列出工作区文件"


def test_config_allow_domain_and_dir(env: Env) -> None:
    extra = env.workspace.parent / "extra"
    extra.mkdir()
    result = invoke("config", "allow-dir", str(extra))
    assert result.exit_code == 0, result.output
    result = invoke("config", "allow-domain", "example.org")
    assert result.exit_code == 0, result.output
    shown = invoke("config", "show")
    assert "example.org" in shown.output
    assert str(extra) in shown.output


def test_config_auto_purge_is_explicit(env: Env) -> None:
    off = invoke("config", "auto-purge", "false")
    assert off.exit_code == 0, off.output
    assert "已关闭" in off.output
    on = invoke("config", "auto-purge", "true")
    assert on.exit_code == 0, on.output
    assert "已开启" in on.output
    shown = json.loads(invoke("config", "show").output)
    assert shown["audit_auto_purge"] is True


def test_config_deny_rules_via_cli(env: Env) -> None:
    added = invoke("config", "deny-name", "secret.txt", "--workspace")
    assert added.exit_code == 0, added.output
    suffix = invoke("config", "deny-suffix", "bak")
    assert suffix.exit_code == 0, suffix.output
    shown = invoke("config", "show")
    assert "secret.txt" in shown.output
    assert ".bak" in shown.output


def test_purge_removes_old_audit_records(env: Env) -> None:
    audit = AuditLog(env.layout)
    audit.create_task("old-task", "old goal", "mcp", str(env.workspace))
    with sqlite3.connect(env.layout.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", "old-task"),
        )
    preview = invoke("purge", "--days", "1")
    assert preview.exit_code == 0, preview.output
    assert "将清理：任务 1 条" in preview.output
    assert "未删除任何记录" in preview.output
    assert audit.get_task("old-task") is not None  # 默认 dry-run，不能真的删

    applied = invoke("purge", "--days", "1", "--apply")
    assert applied.exit_code == 0, applied.output
    assert "已清理 1" in applied.output
    assert audit.get_task("old-task") is None


def test_doctor_cli(env: Env) -> None:
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "FreeAgent 自检：通过" in result.output
    assert "MCP 工具数：18" in result.output
    assert "连接器清单" in result.output
    assert "网页自动化" in result.output

    machine = invoke("doctor", "--json")
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["ok"] is True
    assert {item["id"] for item in payload["connectors"]} == {"manual", "mock", "web"}
    assert payload["audit_auto_purge"] is False


def test_config_web_model_url_rejects_bad_scheme(env: Env) -> None:
    bad = invoke("config", "web-model-url", "javascript:alert(1)")
    assert bad.exit_code == 1
    ok = invoke("config", "web-model-url", "https://chat.deepseek.com")
    assert ok.exit_code == 0, ok.output
    assert json.loads(invoke("config", "show").output)["web_model_url"] == "https://chat.deepseek.com"
