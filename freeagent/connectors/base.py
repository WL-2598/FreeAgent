"""连接器接口：信任边界在连接器之外，返回值一律视为不可信文本。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Health:
    available: bool
    status: str
    detail: str = ""


@dataclass(frozen=True)
class AskContext:
    task_id: str
    step: int
    round_index: int


class Connector(Protocol):
    id: str
    label: str

    def health(self) -> Health: ...

    def ask(self, prompt: str, context: AskContext) -> str: ...

    def login(self) -> str: ...
