"""MCP 适配层测试：桥接逻辑 + 真实 stdio 协议握手。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from freeagent.mcp_server import McpBridge

from .conftest import Env


def bridge(env: Env, *, allow_write: bool) -> McpBridge:
    return McpBridge(env.config, layout=env.layout, allow_write=allow_write)


def test_read_tools_and_write_disabled_by_default(env: Env) -> None:
    (env.workspace / "note.txt").write_text("hello")
    service = bridge(env, allow_write=False)
    listed = service.list(".")
    assert listed["ok"] and "note.txt" in listed["output"]
    read = service.read_text("note.txt", 100)
    assert read["ok"] and read["output"] == "hello"
    assert "不可信数据" in read["note"]

    prepared = service.prepare("fs.write_text", {"path": "new.txt", "content": "x"})
    assert prepared["ok"] is False
    assert "写入未启用" in prepared["error"]
    assert not (env.workspace / "new.txt").exists()


def test_prepare_commit_undo_two_phase(env: Env) -> None:
    service = bridge(env, allow_write=True)
    prepared = service.prepare(
        "fs.write_text", {"path": "note.md", "content": "draft", "overwrite": False}
    )
    assert prepared["ok"]
    token = prepared["token"]
    assert "写入文本" in prepared["preview"]
    assert not (env.workspace / "note.md").exists(), "fa_prepare 不得执行任何变更"

    committed = service.commit(token)
    assert committed["ok"]
    assert (env.workspace / "note.md").read_text() == "draft"
    assert service.commit(token)["ok"] is False, "令牌必须一次性"

    undone = service.undo(token)
    assert undone["ok"]
    assert not (env.workspace / "note.md").exists()
    task = service.audit.get_task(committed["task_id"])
    assert task["status"] == "done"
    assert service.audit.list_actions(committed["task_id"])[0]["decision"] == "approved_mcp"


def test_prepare_rejects_illegal_path_and_tool(env: Env) -> None:
    service = bridge(env, allow_write=True)
    for action, arguments in (
        ("fs.write_text", {"path": "../outside.txt", "content": "x"}),
        ("fs.write_text", {"path": ".env", "content": "x"}),
        ("shell.exec", {"command": "rm -rf /"}),
        ("fs.read_text", {"path": "a.txt"}),  # 非变更动作不能走两阶段
    ):
        result = service.prepare(action, arguments)
        assert result["ok"] is False
        assert "token" not in result
    assert not (env.workspace.parent / "outside.txt").exists()


def test_mcp_stdio_handshake_and_tools(env: Env) -> None:
    pytest.importorskip("mcp")

    async def run() -> tuple[dict[str, object], str]:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        environ = os.environ.copy()
        environ.update(
            FREEAGENT_HOME=str(env.home),
            FREEAGENT_WORKSPACE=str(env.workspace),
            FREEAGENT_MCP_ALLOW_WRITE="0",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "freeagent.mcp_server"],
            env=environ,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("fa_policy", {})
                text = ""
                for part in result.content:
                    text += getattr(part, "text", "") or ""
                return {tool.name: tool.annotations for tool in tools.tools}, text

    annotations, text = asyncio.run(run())
    names = set(annotations)
    expected = {
        "fa_policy", "fa_doctor", "fa_list", "fa_find", "fa_read_text", "fa_browser_open",
        "fa_prepare", "fa_commit", "fa_undo",
        "fa_task_start", "fa_task_reply", "fa_task_approve", "fa_task_reject",
        "fa_task_cancel", "fa_task_status", "fa_task_list", "fa_task_undo",
    }
    assert expected <= names
    assert "写入状态：关闭" in text
    assert "工具策略提示" in text

    def hint(name: str, field: str) -> bool:
        ann = annotations[name]
        return bool(getattr(ann, field))

    assert hint("fa_doctor", "read_only_hint") is True
    assert hint("fa_list", "read_only_hint") is True
    assert hint("fa_find", "read_only_hint") is True
    assert hint("fa_task_approve", "destructive_hint") is True
    assert hint("fa_commit", "destructive_hint") is True
    assert hint("fa_browser_open", "open_world_hint") is True
    assert hint("fa_undo", "destructive_hint") is True


def test_mcp_stdio_task_level_flow(env: Env) -> None:
    pytest.importorskip("mcp")

    async def run() -> dict[str, dict]:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        environ = os.environ.copy()
        environ.update(
            FREEAGENT_HOME=str(env.home),
            FREEAGENT_WORKSPACE=str(env.workspace),
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "freeagent.mcp_server"],
            env=environ,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

        async def call(session: ClientSession, name: str, args: dict) -> dict:
            result = await session.call_tool(name, args)
            text = "".join(getattr(part, "text", "") or "" for part in result.content)
            return json.loads(text)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                started = await call(session, "fa_task_start", {"goal": "列出工作区文件"})
                listed = await call(session, "fa_task_reply", {
                    "task_id": started["task_id"],
                    "response": '{"action":"fs.list","arguments":{"path":"."},"reason":"test"}',
                })
                done = await call(session, "fa_task_reply", {
                    "task_id": started["task_id"],
                    "response": '{"action":"task.done","arguments":{"summary":"完成"},"reason":"test"}',
                })
                status = await call(session, "fa_task_status", {"task_id": started["task_id"]})
                return {"started": started, "listed": listed, "done": done, "status": status}

    result = asyncio.run(run())
    assert result["started"]["status"] == "awaiting_model"
    assert result["listed"]["status"] == "awaiting_model"
    assert result["done"]["status"] == "done"
    assert result["status"]["status"] == "done"


def test_mcp_stdio_task_write_approval_and_undo(env: Env) -> None:
    pytest.importorskip("mcp")

    async def run() -> dict[str, dict]:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        environ = os.environ.copy()
        environ.update(
            FREEAGENT_HOME=str(env.home),
            FREEAGENT_WORKSPACE=str(env.workspace),
            FREEAGENT_MCP_ALLOW_WRITE="1",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "freeagent.mcp_server"],
            env=environ,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

        async def call(session: ClientSession, name: str, args: dict) -> dict:
            result = await session.call_tool(name, args)
            text = "".join(getattr(part, "text", "") or "" for part in result.content)
            return json.loads(text)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                started = await call(session, "fa_task_start", {"goal": "写入一份摘要"})
                pending = await call(session, "fa_task_reply", {
                    "task_id": started["task_id"],
                    "response": '{"action":"fs.write_text","arguments":{"path":"via-task.md","content":"hello","overwrite":false},"reason":"test"}',
                })
                assert not (env.workspace / "via-task.md").exists()
                approved = await call(session, "fa_task_approve", {
                    "task_id": started["task_id"],
                    "approval_token": pending["approval_token"],
                })
                done = await call(session, "fa_task_reply", {
                    "task_id": started["task_id"],
                    "response": '{"action":"task.done","arguments":{"summary":"ok"},"reason":"test"}',
                })
                undone = await call(session, "fa_task_undo", {"task_id": started["task_id"]})
                return {"pending": pending, "approved": approved, "done": done, "undone": undone}

    result = asyncio.run(run())
    assert result["pending"]["status"] == "awaiting_approval"
    assert result["approved"]["status"] == "awaiting_model"
    assert result["done"]["status"] == "done"
    assert result["undone"]["ok"] is True
    assert not (env.workspace / "via-task.md").exists()


def test_mcp_stdio_two_phase_commit_and_undo(env: Env) -> None:
    pytest.importorskip("mcp")

    async def run() -> tuple[str, str, str]:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        environ = os.environ.copy()
        environ.update(
            FREEAGENT_HOME=str(env.home),
            FREEAGENT_WORKSPACE=str(env.workspace),
            FREEAGENT_MCP_ALLOW_WRITE="1",
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "freeagent.mcp_server"],
            env=environ,
            cwd=str(Path(__file__).resolve().parents[1]),
        )

        def payload(result: object) -> str:
            text = ""
            for part in getattr(result, "content", []):
                text += getattr(part, "text", "") or ""
            return text

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                prepared = payload(await session.call_tool(
                    "fa_prepare",
                    {"action": "fs.write_text", "arguments": {
                        "path": "via-mcp.txt", "content": "from openclaw", "overwrite": False,
                    }},
                ))
                assert not (env.workspace / "via-mcp.txt").exists()
                token = json.loads(prepared)["token"]
                committed = payload(await session.call_tool("fa_commit", {"token": token}))
                undone = payload(await session.call_tool("fa_undo", {"token": token}))
                return prepared, committed, undone

    prepared, committed, undone = asyncio.run(run())
    assert json.loads(prepared)["ok"] is True
    assert json.loads(committed)["ok"] is True
    assert json.loads(undone)["ok"] is True
    assert not (env.workspace / "via-mcp.txt").exists(), "undo 后文件应恢复"
