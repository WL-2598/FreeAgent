"""结构化动作协议。

模型只能返回这里定义的动作之一；Pydantic 严格模式拒绝未知动作/未知字段。
模型文本绝不拼接进 shell；工具由本地注册表按名字调用。
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .errors import ParseError
from .security import log_text

MAX_CONTENT_CHARS = 20_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FsListArgs(StrictModel):
    path: str = Field(default=".", max_length=4096, description="相对工作区的目录")


class FsFindArgs(StrictModel):
    path: str = Field(default=".", max_length=4096, description="搜索起点（工作区内）")
    pattern: str = Field(default="*", max_length=200, description="文件名通配符，例如 *.pdf")
    max_results: int = Field(default=100, ge=1, le=500)


class FsReadTextArgs(StrictModel):
    path: str = Field(max_length=4096)
    max_chars: int = Field(default=4000, ge=1, le=20_000)


class FsMkdirArgs(StrictModel):
    path: str = Field(max_length=4096)


class FsMoveArgs(StrictModel):
    source: str = Field(max_length=4096)
    destination: str = Field(max_length=4096)


class FsWriteTextArgs(StrictModel):
    path: str = Field(max_length=4096)
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    overwrite: bool = Field(default=False, description="覆盖已有文件须显式为 true 且仍须用户确认")


class BrowserOpenArgs(StrictModel):
    url: str = Field(max_length=2048)


class TaskDoneArgs(StrictModel):
    summary: str = Field(default="", max_length=4000)


class TaskBlockedArgs(StrictModel):
    reason: str = Field(max_length=2000)


class FsList(StrictModel):
    action: Literal["fs.list"] = "fs.list"
    arguments: FsListArgs = Field(default_factory=FsListArgs)
    reason: str = Field(default="", max_length=300)


class FsFind(StrictModel):
    action: Literal["fs.find"] = "fs.find"
    arguments: FsFindArgs = Field(default_factory=FsFindArgs)
    reason: str = Field(default="", max_length=300)


class FsReadText(StrictModel):
    action: Literal["fs.read_text"] = "fs.read_text"
    arguments: FsReadTextArgs
    reason: str = Field(default="", max_length=300)


class FsMkdir(StrictModel):
    action: Literal["fs.mkdir"] = "fs.mkdir"
    arguments: FsMkdirArgs
    reason: str = Field(default="", max_length=300)


class FsMove(StrictModel):
    action: Literal["fs.move"] = "fs.move"
    arguments: FsMoveArgs
    reason: str = Field(default="", max_length=300)


class FsWriteText(StrictModel):
    action: Literal["fs.write_text"] = "fs.write_text"
    arguments: FsWriteTextArgs
    reason: str = Field(default="", max_length=300)


class BrowserOpen(StrictModel):
    action: Literal["browser.open"] = "browser.open"
    arguments: BrowserOpenArgs
    reason: str = Field(default="", max_length=300)


class TaskDone(StrictModel):
    action: Literal["task.done"] = "task.done"
    arguments: TaskDoneArgs = Field(default_factory=TaskDoneArgs)
    reason: str = Field(default="", max_length=300)


class TaskBlocked(StrictModel):
    action: Literal["task.blocked"] = "task.blocked"
    arguments: TaskBlockedArgs
    reason: str = Field(default="", max_length=300)


Action = Annotated[
    Union[
        FsList, FsFind, FsReadText, FsMkdir, FsMove, FsWriteText, BrowserOpen, TaskDone, TaskBlocked
    ],
    Field(discriminator="action"),
]

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)

MUTATING_ACTIONS = frozenset({"fs.mkdir", "fs.move", "fs.write_text"})
CONFIRM_ACTIONS = frozenset(MUTATING_ACTIONS | {"fs.read_text", "browser.open"})
TERMINAL_ACTIONS = frozenset({"task.done", "task.blocked"})
TOOL_ACTIONS = frozenset(
    {"fs.list", "fs.find", "fs.read_text", "fs.mkdir", "fs.move", "fs.write_text", "browser.open"}
)

TOOL_SPECS: dict[str, str] = {
    "fs.list": '{"action":"fs.list","arguments":{"path":"."},"reason":"..."}；列出目录条目，不读内容',
    "fs.find": '{"action":"fs.find","arguments":{"path":".","pattern":"*.pdf","max_results":100},"reason":"..."}；按文件名递归查找，不读内容',
    "fs.read_text": '{"action":"fs.read_text","arguments":{"path":"相对路径","max_chars":4000},"reason":"..."}；需用户批准',
    "fs.mkdir": '{"action":"fs.mkdir","arguments":{"path":"相对路径"},"reason":"..."}；需用户批准',
    "fs.move": '{"action":"fs.move","arguments":{"source":"a.txt","destination":"归档/a.txt"},"reason":"..."}；需用户批准',
    "fs.write_text": '{"action":"fs.write_text","arguments":{"path":"摘要.md","content":"...","overwrite":false},"reason":"..."}；需用户批准',
    "browser.open": '{"action":"browser.open","arguments":{"url":"https://..."},"reason":"..."}；只打开页面，不自动点击/提交，需用户批准',
    "task.done": '{"action":"task.done","arguments":{"summary":"完成说明"},"reason":"..."}',
    "task.blocked": '{"action":"task.blocked","arguments":{"reason":"无法完成的原因"},"reason":"..."}',
}


def validate_action(value: object) -> Action:
    """严格校验动作；未知字段/未知动作/超长内容会被拒绝。"""
    return _ACTION_ADAPTER.validate_python(value)


def action_name(action: Action) -> str:
    return action.action


def action_args(action: Action) -> dict:
    return action.arguments.model_dump(mode="json")


def is_mutating(action: Action) -> bool:
    return action.action in MUTATING_ACTIONS


def requires_confirmation(action: Action) -> bool:
    return action.action in CONFIRM_ACTIONS


def describe_action(action: Action) -> str:
    args = action_args(action)
    shown: dict[str, object] = {}
    for key, value in args.items():
        if key == "content":
            shown[key] = f"<{len(str(value))} 字符>"
        else:
            shown[key] = value
    payload = json.dumps(shown, ensure_ascii=False)
    return f"{action.action} {payload}" + (f"\n理由：{action.reason}" if action.reason else "")


def _json_objects(text: str) -> list[str]:
    """提取顶层完整 JSON 对象，忽略 fenced code block 与前后说明文字。"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            if depth == 0:
                start = index
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objects.append(cleaned[start : index + 1])
                    start = -1
    return objects


def parse_action(text: str) -> Action:
    """从模型原始回复中解析出唯一一个动作。失败时抛出 ParseError。"""
    if not isinstance(text, str) or not text.strip():
        raise ParseError("模型回复为空")
    candidates = _json_objects(text)
    if not candidates:
        raise ParseError(f"模型回复中没有 JSON 对象；原文片段：{log_text(text, 300)}")
    if len(candidates) > 1:
        raise ParseError("模型回复包含多个 JSON 对象；每轮只能提出一个动作")
    try:
        raw = json.loads(candidates[0])
    except json.JSONDecodeError as exc:
        raise ParseError(f"JSON 语法错误：{exc}；原文片段：{log_text(candidates[0], 300)}") from exc
    try:
        return validate_action(raw)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(str(x) for x in item['loc'])}: {item['msg']}" for item in exc.errors())
        raise ParseError(f"动作不符合协议：{log_text(errors, 600)}") from exc
