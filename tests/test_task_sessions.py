"""任务级 MCP 会话测试：外部智能体只传模型回复，本地保留所有安全决策。"""

from __future__ import annotations

import json

from freeagent.audit import AuditLog
from freeagent.task_session import TaskSessions

from .conftest import Env, action


def sessions(env: Env, *, allow_write: bool = True, allow_reads: bool = False) -> TaskSessions:
    return TaskSessions(
        env.config, AuditLog(env.layout),
        allow_write=allow_write, allow_reads=allow_reads,
    )


def test_full_task_flow_with_approval_and_undo(env: Env) -> None:
    service = sessions(env)
    started = service.start("列出文件并写一份摘要")
    assert started["ok"] and started["status"] == "awaiting_model"
    assert "fs.list" in started["prompt"]
    task_id = started["task_id"]

    listed = service.reply(task_id, action("fs.list", path="."))
    assert listed["status"] == "awaiting_model"
    assert "awaiting_model" in listed["next_prompt"] or "请只输出一个" in listed["next_prompt"]

    prepared = service.reply(
        task_id, action("fs.write_text", path="summary.md", content="# 摘要")
    )
    assert prepared["status"] == "awaiting_approval"
    assert "写入文本" in prepared["preview"]
    token = prepared["approval_token"]
    assert not (env.workspace / "summary.md").exists(), "预览阶段不得写入"

    wrong = service.approve(task_id, "wrong-token")
    assert wrong["ok"] is False
    assert not (env.workspace / "summary.md").exists()

    approved = service.approve(task_id, token, approver="claude-test")
    assert approved["status"] == "awaiting_model"
    assert (env.workspace / "summary.md").read_text() == "# 摘要"
    assert approved["undo_available"] is True
    assert approved["approver"] == "claude-test"
    assert len(approved["preview_hash"]) == 64
    assert service.approve(task_id, token)["ok"] is False, "批准令牌必须一次性"

    finished = service.reply(task_id, action("task.done", summary="完成"))
    assert finished["status"] == "done"
    status = service.status(task_id)
    assert status["status"] == "done"
    assert [row["decision"] for row in status["actions"]] == ["auto", "pending", "approved_mcp", "auto"]
    approved_row = next(row for row in status["actions"] if row["decision"] == "approved_mcp")
    assert approved_row["approver"] == "claude-test"
    assert approved_row["preview_hash"] == approved["preview_hash"]

    undone = service.undo(task_id)
    assert undone["ok"] and not (env.workspace / "summary.md").exists()


def test_invalid_action_stops_without_changes(env: Env) -> None:
    (env.workspace.parent / "outside.txt").write_text("safe")
    task_id = sessions(env).start("越权测试")["task_id"]
    service = sessions(env)
    result = service.reply(
        task_id, action("fs.write_text", path="../outside.txt", content="pwned")
    )
    assert result["ok"] is False
    assert service.status(task_id)["status"] == "blocked"
    assert (env.workspace.parent / "outside.txt").read_text() == "safe"
    assert not (env.workspace / "pwned").exists()


def test_parse_error_can_be_retried_within_budget(env: Env) -> None:
    task_id = sessions(env).start("格式错误测试")["task_id"]
    service = sessions(env)
    first = service.reply(task_id, "这不是 JSON")
    assert first["ok"] is False
    assert first["status"] == "awaiting_model"
    assert "请只输出一个" in first["prompt"]
    assert service.status(task_id)["actions"] == []

    second = service.reply(task_id, action("task.done", summary="修正后完成"))
    assert second["status"] == "done"


def test_reject_and_cancel_leave_no_changes(env: Env) -> None:
    service = sessions(env)
    task_id = service.start("拒绝测试")["task_id"]
    pending = service.reply(task_id, action("fs.mkdir", path="newdir"))
    assert pending["status"] == "awaiting_approval"
    rejected = service.reject(task_id)
    assert rejected["status"] == "cancelled"
    assert not (env.workspace / "newdir").exists()

    other = service.start("取消测试")["task_id"]
    assert service.cancel(other)["status"] == "cancelled"
    assert service.reply(other, action("fs.list"))["ok"] is False


def test_write_is_blocked_even_if_approve_tool_is_available(env: Env) -> None:
    service = sessions(env, allow_write=False)
    task_id = service.start("只读配置下的写入测试")["task_id"]
    result = service.reply(
        task_id, action("fs.write_text", path="nope.md", content="x")
    )
    assert result["ok"] is False
    assert "写入未启用" in result["error"]
    assert service.status(task_id)["status"] == "blocked"
    assert not (env.workspace / "nope.md").exists()
    assert service.approve(task_id, "anything")["ok"] is False


def test_explicit_read_flag_allows_file_content_in_read_only_flow(env: Env) -> None:
    (env.workspace / "note.txt").write_text("hello from file")
    service = sessions(env, allow_write=False, allow_reads=True)
    task_id = service.start("读取文件内容")["task_id"]
    result = service.reply(task_id, action("fs.read_text", path="note.txt", max_chars=100))
    assert result["status"] == "awaiting_model"
    assert "approval_token" not in result
    assert "hello from file" in result["next_prompt"]


def test_approval_token_survives_service_restart_until_used(env: Env) -> None:
    first = sessions(env)
    task_id = first.start("重启后批准测试")["task_id"]
    pending = first.reply(
        task_id, action("fs.write_text", path="after-restart.txt", content="ok")
    )
    token = pending["approval_token"]
    assert not (env.workspace / "after-restart.txt").exists()

    restarted = sessions(env)  # 模拟 MCP 服务进程重启
    approved = restarted.approve(task_id, token)
    assert approved["status"] == "awaiting_model"
    assert (env.workspace / "after-restart.txt").read_text() == "ok"
    assert restarted.approve(task_id, token)["ok"] is False  # 一次性


def test_task_state_survives_new_session_instance(env: Env) -> None:
    first = sessions(env)
    task_id = first.start("跨实例状态测试")["task_id"]
    first.reply(task_id, action("fs.list", path="."))

    second = sessions(env)  # 模拟 MCP 服务重启后新建会话管理器
    status = second.status(task_id)
    assert status["status"] == "awaiting_model"
    assert status["step"] == 1
    assert len(status["actions"]) == 1

    # 批准令牌只存内存，重启后不能凭旧令牌执行（旧会话令牌不存在）。
    pending = second.reply(task_id, action("fs.mkdir", path="fresh"))
    assert pending["status"] == "awaiting_approval"
    assert second.approve(task_id, "stale-token")["ok"] is False
    assert not (env.workspace / "fresh").exists()


def test_status_returns_next_prompt_for_cross_process_resume(env: Env) -> None:
    service = sessions(env)
    task_id = service.start("续跑测试")["task_id"]
    service.reply(task_id, action("fs.list", path="."))

    status = service.status(task_id)
    assert status["status"] == "awaiting_model"
    assert "next_prompt" in status
    assert "已执行动作摘要" in status["next_prompt"]

    service.reply(task_id, action("task.done", summary="完成"))
    assert "next_prompt" not in service.status(task_id)
