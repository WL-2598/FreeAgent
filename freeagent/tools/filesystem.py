"""受限文件系统工具：全部路径经 Policy 校验，全部变更可撤销。"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..errors import PolicyError, ToolError
from ..policy import Policy
from ..protocol import Action, FsList, FsMkdir, FsMove, FsReadText, FsWriteText
from ..security import log_text, truncate
from ..utils import atomic_write_text
from .base import Execution, Preview


def _path_arg(action: Action, key: str = "path") -> str:
    return getattr(action.arguments, key)


class FsListTool:
    name = "fs.list"

    def preview(self, action: Action, policy: Policy) -> Preview:
        target = policy.path(_path_arg(action), "list")
        return Preview([f"列出目录：{target}"], [str(target)], "低（不读取内容）")

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution:
        target = policy.path(_path_arg(action), "list")
        if not target.exists():
            raise ToolError(f"目录不存在：{target}")
        if not target.is_dir():
            raise ToolError(f"不是目录：{target}")
        try:
            entries = sorted(item for item in target.iterdir() if not item.name.startswith("."))
        except OSError as exc:
            raise ToolError(f"无法列出目录：{exc}") from exc
        lines: list[str] = []
        for item in entries[:200]:
            try:
                stat = item.stat()
                kind = "dir" if item.is_dir() else "file"
                stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
                lines.append(f"{kind}\t{stamp}\t{stat.st_size}\t{item.name}")
            except OSError:
                continue
        if len(entries) > 200:
            lines.append(f"…[仅显示前 200 项，共 {len(entries)} 项]")
        return Execution("\n".join(lines), f"列出 {len(lines)} 项：{target}")


class FsReadTextTool:
    name = "fs.read_text"

    def preview(self, action: Action, policy: Policy) -> Preview:
        target = policy.path(_path_arg(action), "read")
        return Preview(
            [
                f"读取文本：{target}",
                f"最多上传 {action.arguments.max_chars} 字符给网页模型（并受全局上限约束）",
            ],
            [str(target)],
            "中（内容会发送给网页模型）",
        )

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution:
        assert isinstance(action, FsReadText)
        target = policy.path(action.arguments.path, "read")
        if not target.exists():
            raise ToolError(f"文件不存在：{target}")
        if not target.is_file():
            raise ToolError(f"不是普通文件：{target}")
        try:
            size = target.stat().st_size
            if size > config.max_file_bytes:
                raise ToolError(f"文件过大：{size} 字节 > 上限 {config.max_file_bytes}")
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc
        limit = min(action.arguments.max_chars, config.max_output_chars)
        return Execution(truncate(text, limit), f"读取 {len(text)} 字符：{target}")


class FsMkdirTool:
    name = "fs.mkdir"

    def preview(self, action: Action, policy: Policy) -> Preview:
        target = policy.path(_path_arg(action), "mkdir")
        return Preview([f"新建目录：{target}"], [str(target)], "低")

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution:
        assert isinstance(action, FsMkdir)
        target = policy.path(action.arguments.path, "mkdir")
        if target.exists():
            raise ToolError(f"目标已存在：{target}")
        if not target.parent.is_dir():
            raise ToolError(f"父目录不存在：{target.parent}")
        try:
            target.mkdir(mode=0o755)
        except OSError as exc:
            raise ToolError(f"创建目录失败：{exc}") from exc
        return Execution(
            f"已创建目录：{target}",
            f"已创建目录：{target}",
            {"kind": "mkdir", "path": str(target)},
        )


class FsMoveTool:
    name = "fs.move"

    def preview(self, action: Action, policy: Policy) -> Preview:
        assert isinstance(action, FsMove)
        source = policy.path(action.arguments.source, "move_source")
        destination = policy.path(action.arguments.destination, "move_dest")
        lines = [f"移动：{source}  →  {destination}"]
        if destination.exists():
            lines.append("警告：目标已存在，本次操作将中止")
        return Preview(lines, [str(source), str(destination)], "中（可撤销）")

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution:
        assert isinstance(action, FsMove)
        source = policy.path(action.arguments.source, "move_source")
        destination = policy.path(action.arguments.destination, "move_dest")
        if not source.exists() or not source.is_file():
            raise ToolError(f"源文件不存在或不是普通文件：{source}")
        if destination.exists():
            raise ToolError(f"目标已存在，拒绝覆盖：{destination}")
        if not destination.parent.is_dir():
            raise ToolError(f"目标父目录不存在：{destination.parent}")
        if source == destination:
            raise ToolError("源路径与目标路径相同")
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            raise ToolError(f"移动失败：{exc}") from exc
        return Execution(
            f"已移动：{source} -> {destination}",
            f"已移动：{source} → {destination}",
            {"kind": "move", "source": str(source), "destination": str(destination)},
        )


class FsWriteTextTool:
    name = "fs.write_text"

    def preview(self, action: Action, policy: Policy) -> Preview:
        assert isinstance(action, FsWriteText)
        target = policy.path(action.arguments.path, "write")
        content = action.arguments.content
        lines = [
            f"写入文本：{target}",
            f"内容长度：{len(content)} 字符；覆盖：{'是' if action.arguments.overwrite else '否'}",
            "内容预览：" + log_text(truncate(content.replace("\n", " ⏎ "), 160), 200),
        ]
        if target.exists():
            lines.append("警告：目标已存在，覆盖需 overwrite=true 且明确确认")
        return Preview(lines, [str(target)], "中（可撤销）")

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution:
        assert isinstance(action, FsWriteText)
        target = policy.path(action.arguments.path, "write")
        if target.exists() and not action.arguments.overwrite:
            raise ToolError(f"文件已存在且 overwrite=false，拒绝覆盖：{target}")
        if target.is_dir():
            raise ToolError(f"目标是目录：{target}")
        if len(action.arguments.content.encode("utf-8")) > config.max_file_bytes:
            raise ToolError(f"写入内容超过单文件大小上限 {config.max_file_bytes} 字节")
        previous: str | None = None
        previous_mode = 0o644
        if target.exists():
            try:
                stat = target.stat()
                previous_mode = stat.st_mode & 0o777
                if stat.st_size <= config.max_file_bytes:
                    previous = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ToolError(f"无法读取原内容用于撤销：{exc}") from exc
            else:
                if stat.st_size > config.max_file_bytes:
                    raise ToolError("原文件过大，无法安全记录撤销信息；拒绝覆盖")
        try:
            atomic_write_text(target, action.arguments.content, mode=0o644)
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc
        return Execution(
            f"已写入 {len(action.arguments.content)} 字符：{target}",
            f"已写入：{target}",
            {
                "kind": "write",
                "path": str(target),
                "previous_content": previous,
                "previous_mode": previous_mode,
            },
        )


def undo_change(record: dict, config: Config, policy: Policy) -> str:
    """按审计记录撤销单项变更，撤销时也重新走路径策略。"""
    kind = record.get("kind")
    if kind == "mkdir":
        target = policy.path(record["path"], "mkdir")
        if not target.is_dir() or any(target.iterdir()):
            raise ToolError(f"目录不存在或非空，无法撤销：{target}")
        target.rmdir()
        return f"已移除空目录：{target}"
    if kind == "move":
        # 记录中的 source 是原始位置，destination 是当前位置；撤销方向相反。
        current = policy.path(record["destination"], "move_source")
        original = policy.path(record["source"], "move_dest")
        if not current.is_file() or original.exists():
            raise ToolError(f"无法撤销移动：{current} -> {original}")
        shutil.move(str(current), str(original))
        return f"已还原移动：{current} → {original}"
    if kind == "write":
        target = policy.path(record["path"], "write")
        previous = record.get("previous_content")
        if previous is None:
            if target.is_dir():
                raise ToolError(f"无法撤销写入：{target} 是目录")
            target.unlink(missing_ok=True)
            return f"已删除新建文件：{target}"
        atomic_write_text(target, previous, mode=int(record.get("previous_mode", 0o644)))
        return f"已恢复原内容：{target}"
    raise ToolError(f"未知的撤销记录类型：{kind}")
