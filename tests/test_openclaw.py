"""OpenClaw 自动驾驶接入：技能安装、任务简报、令牌重签发。"""

from __future__ import annotations

import pytest

from freeagent.audit import AuditLog
from freeagent.openclaw import SKILL_MD, SKILL_NAME, install_skill, render_brief
from freeagent.task_session import TaskSessions

from .conftest import Env, action


def test_install_skill_writes_once_and_requires_force(tmp_path) -> None:
    path = install_skill(tmp_path)
    assert path == tmp_path / SKILL_NAME / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "name: freeagent-web-loop" in text
    # 技能本身必须写清红线，否则 OpenClaw 可能把它当普通工具说明。
    for required in ("fa_task_approve", "不要绕过", "验证码", "fa_task_reissue"):
        assert required in text

    with pytest.raises(FileExistsError):
        install_skill(tmp_path)
    replaced = install_skill(tmp_path, force=True)
    assert replaced.read_text(encoding="utf-8") == SKILL_MD


def test_render_brief_carries_safety_language() -> None:
    brief = render_brief("把 PDF 归档", "https://chat.deepseek.com")
    assert "freeagent-web-loop" in brief
    assert "把 PDF 归档" in brief
    assert "https://chat.deepseek.com" in brief
    assert "不要绕过" in brief
    # 空任务不能生成一个看起来能跑的指令。
    assert "<在这里写你的任务>" in render_brief("   ", "https://example.org")


def test_reissue_keeps_action_unexecuted(env: Env) -> None:
    service = TaskSessions(env.config, AuditLog(env.layout), allow_write=True)
    task_id = service.start("写一份摘要")["task_id"]
    pending = service.reply(task_id, action("fs.write_text", path="note.md", content="hello"))
    assert pending["status"] == "awaiting_approval"

    reissued = service.reissue(task_id)
    assert reissued["ok"] is True
    assert reissued["status"] == "awaiting_approval"
    assert not (env.workspace / "note.md").exists(), "重签发不得执行动作"
    assert reissued["approval_token"] != pending["approval_token"], "应换发新令牌"
    assert "写入文本" in reissued["preview"]

    # 旧令牌作废，新令牌可用且仍是一次性。
    assert service.approve(task_id, pending["approval_token"])["ok"] is False
    approved = service.approve(task_id, reissued["approval_token"], approver="user")
    assert approved["ok"] is True
    assert (env.workspace / "note.md").read_text(encoding="utf-8") == "hello"
    assert service.approve(task_id, reissued["approval_token"])["ok"] is False


def test_reissue_rejects_tasks_without_pending_action(env: Env) -> None:
    service = TaskSessions(env.config, AuditLog(env.layout), allow_write=True)
    task_id = service.start("列文件")["task_id"]
    assert service.reissue(task_id)["ok"] is False
    assert service.reissue("mt-missing")["ok"] is False


def test_task_start_returns_driver_hints(env: Env) -> None:
    service = TaskSessions(env.config, AuditLog(env.layout))
    started = service.start("列文件")
    driver = started["driver"]
    assert driver["web_model_url"] == env.config.web_model_url
    assert driver["profile"] == "openclaw"
    assert any("验证码" in item for item in driver["stop_conditions"])
    assert any("fa_task_approve" in item for item in driver["never"])


def test_cli_installs_skill_and_reports_existing(env: Env, tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from freeagent.cli import app

    skills = tmp_path / "skills"
    runner = CliRunner()
    first = runner.invoke(app, ["openclaw", "install-skill", "--dir", str(skills)])
    assert first.exit_code == 0, first.output
    assert (skills / "freeagent-web-loop" / "SKILL.md").exists()

    second = runner.invoke(app, ["openclaw", "install-skill", "--dir", str(skills)])
    assert second.exit_code == 1
    assert "--force" in second.output

    shown = runner.invoke(app, ["openclaw", "install-skill", "--show"])
    assert shown.exit_code == 0, shown.output
    assert "fa_task_reissue" in shown.output
