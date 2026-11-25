from __future__ import annotations

import json
from pathlib import Path

import pytest

from freeagent.config import Config
from freeagent.errors import PolicyError, ToolError
from freeagent.policy import Policy
from freeagent.protocol import parse_action
from freeagent.tools import default_registry, undo_change

from .conftest import Env, action


def run_tool(env: Env, raw: str):
    action_obj = parse_action(raw)
    registry = default_registry()
    tool = registry.get(action_obj.action)
    preview = tool.preview(action_obj, Policy(env.config))
    execution = tool.execute(action_obj, Policy(env.config), env.config)
    return preview, execution


def test_list_read_write_move_and_undo(env: Env) -> None:
    docs = env.workspace / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha\n")
    _, listed = run_tool(env, action("fs.list", path="."))
    assert "docs" in listed.output
    assert "alpha" not in listed.output  # fs.list 不读内容

    _, read = run_tool(env, action("fs.read_text", path="docs/a.txt", max_chars=10))
    assert read.output == "alpha\n"

    _, mkdir = run_tool(env, action("fs.mkdir", path="archive"))
    assert (env.workspace / "archive").is_dir()

    _, moved = run_tool(env, action("fs.move", source="docs/a.txt", destination="archive/a.txt"))
    assert moved.undo is not None
    assert (env.workspace / "archive/a.txt").read_text() == "alpha\n"
    assert not (docs / "a.txt").exists()

    undo_change(moved.undo, env.config, Policy(env.config))
    assert (docs / "a.txt").read_text() == "alpha\n"
    assert not (env.workspace / "archive/a.txt").exists()


def test_write_overwrite_and_undo(env: Env) -> None:
    _, created = run_tool(env, action("fs.write_text", path="note.md", content="new\n"))
    assert (env.workspace / "note.md").read_text() == "new\n"
    with pytest.raises(ToolError):
        run_tool(env, action("fs.write_text", path="note.md", content="again\n"))
    _, updated = run_tool(env, action("fs.write_text", path="note.md", content="changed\n", overwrite=True))
    assert updated.undo is not None
    assert updated.undo["previous_content"] == "new\n"
    undo_change(updated.undo, env.config, Policy(env.config))
    assert (env.workspace / "note.md").read_text() == "new\n"


def test_move_never_overwrites(env: Env) -> None:
    (env.workspace / "a.txt").write_text("a")
    (env.workspace / "b.txt").write_text("b")
    with pytest.raises(ToolError):
        run_tool(env, action("fs.move", source="a.txt", destination="b.txt"))
    assert (env.workspace / "a.txt").read_text() == "a"
    assert (env.workspace / "b.txt").read_text() == "b"


def test_read_size_limit_and_truncation(env: Env) -> None:
    (env.workspace / "big.txt").write_text("x" * 1000)
    cfg = env.config.model_copy(update={"max_file_bytes": 100})
    from freeagent.protocol import parse_action as parse

    tool = default_registry().get("fs.read_text")
    with pytest.raises(ToolError):
        tool.execute(parse(action("fs.read_text", path="big.txt")), Policy(cfg), cfg)
    result = tool.execute(parse(action("fs.read_text", path="big.txt", max_chars=30)), Policy(env.config), env.config)
    assert len(result.output) < 100
    assert "已截断" in result.output


def test_symlink_escape_cannot_be_written(env: Env) -> None:
    outside = env.workspace.parent / "outside.txt"
    outside.write_text("secret")
    (env.workspace / "escape.txt").symlink_to(outside)
    with pytest.raises(PolicyError):
        run_tool(env, action("fs.write_text", path="escape.txt", content="pwned"))
    assert outside.read_text() == "secret"


def test_browser_open_uses_allowlist_only(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, **kw: opened.append(url) or True)
    _, result = run_tool(env, action("browser.open", url="https://example.com/page"))
    assert opened == ["https://example.com/page"]
    assert "页面内容未自动读取" in result.output
    with pytest.raises(PolicyError):
        run_tool(env, action("browser.open", url="https://evil.example.org.evil.com/"))
    assert len(opened) == 1
