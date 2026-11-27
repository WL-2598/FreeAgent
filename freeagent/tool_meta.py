"""工具策略元数据：供 MCP 客户端（OpenClaw）做细粒度工具过滤。

这些提示不会替代本地策略校验；它们只是让宿主在「工具发现和授权」阶段
就能区分只读、破坏性和外部世界工具。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPolicy:
    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False

    def as_mcp_kwargs(self) -> dict[str, bool]:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


TOOL_POLICY: dict[str, ToolPolicy] = {
    "fa_policy": ToolPolicy(True, idempotent=True),
    "fa_doctor": ToolPolicy(True, idempotent=True),
    "fa_list": ToolPolicy(True, idempotent=True),
    "fa_find": ToolPolicy(True, idempotent=True),
    "fa_read_text": ToolPolicy(True, idempotent=True),
    # 只打开可见标签页，不抓取内容，但确实触达外部世界。
    "fa_browser_open": ToolPolicy(False, idempotent=True, open_world=True),
    # prepare 只是预览 + 发令牌，不产生文件变更。
    "fa_prepare": ToolPolicy(True),
    "fa_commit": ToolPolicy(False, destructive=True),
    "fa_undo": ToolPolicy(False, destructive=True),
    "fa_task_start": ToolPolicy(True),
    # reply 可能执行只读工具，或只产生预览，不改文件。
    "fa_task_reply": ToolPolicy(True),
    "fa_task_approve": ToolPolicy(False, destructive=True),
    # 只重签发「预览中」动作的令牌，不执行任何变更，所以是只读的。
    "fa_task_reissue": ToolPolicy(True),
    "fa_task_reject": ToolPolicy(False),
    "fa_task_cancel": ToolPolicy(False),
    "fa_task_list": ToolPolicy(True, idempotent=True),
    "fa_task_status": ToolPolicy(True, idempotent=True),
    "fa_task_undo": ToolPolicy(False, destructive=True),
}


def policy_for(name: str) -> ToolPolicy:
    return TOOL_POLICY.get(name, ToolPolicy(False))


def policy_summary() -> str:
    readonly = ", ".join(name for name, p in TOOL_POLICY.items() if p.read_only)
    destructive = ", ".join(name for name, p in TOOL_POLICY.items() if p.destructive)
    external = ", ".join(name for name, p in TOOL_POLICY.items() if p.open_world)
    return (
        f"工具策略提示：只读工具 [{readonly}]；"
        f"破坏性工具 [{destructive}]；"
        f"触达外部世界的工具 [{external or '无'}]。"
    )
