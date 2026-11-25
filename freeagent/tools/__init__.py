"""白名单工具注册表。"""

from .base import Execution, Preview, Tool, ToolRegistry
from .browser import BrowserOpenTool
from .filesystem import (
    FsListTool,
    FsMkdirTool,
    FsMoveTool,
    FsReadTextTool,
    FsWriteTextTool,
    undo_change,
)


def default_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            FsListTool(),
            FsReadTextTool(),
            FsMkdirTool(),
            FsMoveTool(),
            FsWriteTextTool(),
            BrowserOpenTool(),
        ]
    )


__all__ = [
    "Execution",
    "Preview",
    "Tool",
    "ToolRegistry",
    "default_registry",
    "undo_change",
]
