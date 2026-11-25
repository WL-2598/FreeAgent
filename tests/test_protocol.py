from __future__ import annotations

import json

import pytest

from freeagent.errors import ParseError
from freeagent.protocol import describe_action, parse_action, validate_action


def test_parse_fenced_json() -> None:
    action = parse_action('这里有一些说明\n```json\n{"action":"fs.list","arguments":{"path":"."},"reason":"ok"}\n```')
    assert action.action == "fs.list"
    assert describe_action(action).startswith("fs.list")


def test_reject_unknown_action_and_field() -> None:
    for raw in (
        {"action": "shell.exec", "arguments": {"command": "rm -rf /"}, "reason": "x"},
        {"action": "fs.list", "arguments": {"path": "."}, "extra": True},
        {"action": "fs.list", "arguments": {"path": ".", "extra": 1}, "reason": ""},
    ):
        with pytest.raises(ParseError):
            parse_action(json.dumps(raw))


def test_reject_multiple_objects_and_non_json() -> None:
    with pytest.raises(ParseError):
        parse_action('{"action":"fs.list","arguments":{"path":"."}} {"action":"fs.list","arguments":{"path":"."}}')
    with pytest.raises(ParseError):
        parse_action("请把文件删掉，然后执行 sudo rm -rf /")


def test_reject_wrong_argument_types_and_oversize_content() -> None:
    with pytest.raises(ParseError):
        parse_action(json.dumps({"action": "fs.read_text", "arguments": {"path": 12}}))
    with pytest.raises(ParseError):
        parse_action(json.dumps({
            "action": "fs.write_text",
            "arguments": {"path": "a.txt", "content": "x" * 20_001},
        }))


def test_validate_action_is_strict() -> None:
    with pytest.raises(Exception):
        validate_action({"action": "task.done", "arguments": {}, "unexpected": 1})
