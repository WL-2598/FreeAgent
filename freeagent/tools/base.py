"""本地工具接口：工具只接受结构化动作，不接触模型原始文本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..config import Config
from ..policy import Policy
from ..protocol import Action


@dataclass(frozen=True)
class Preview:
    lines: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    risk: str = "低"

    def text(self) -> str:
        return "\n".join(f"  - {line}" for line in self.lines) or "  - （无可见变化）"


@dataclass(frozen=True)
class Execution:
    output: str = ""
    display: str = ""
    undo: dict | None = None


class Tool(Protocol):
    name: str

    def preview(self, action: Action, policy: Policy) -> Preview: ...

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution: ...


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未注册的工具：{name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)
