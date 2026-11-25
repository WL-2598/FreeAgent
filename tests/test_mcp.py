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

    async def run() -> tuple[set[str], str]:
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
                return {tool.name for tool in tools.tools}, text

    names, text = asyncio.run(run())
    assert {"fa_policy", "fa_list", "fa_read_text", "fa_browser_open", "fa_prepare", "fa_commit", "fa_undo"} <= names
    assert "写入状态：关闭" in text


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
