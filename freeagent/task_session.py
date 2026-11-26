"""任务级 MCP 会话：让外部智能体（如 OpenClaw）只充当模型传输层。

FreeAgent 保留全部安全决策：提示词、单步动作、策略校验、预览批准、执行与审计。
会话状态保存在已有 tasks 表中，刷新/重启后仍可用 `fa_task_status` 查看历史；
批准令牌只把哈希写入 SQLite（明文不落盘），重启后仍在有效期内可批准，用过即失效。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from typing import Any

from .audit import AuditLog
from .config import Config, workspace_path
from .errors import ActionError, FreeAgentError, ParseError
from .orchestrator import time_limit
from .planner import build_prompt
from .policy import Policy
from .protocol import (
    TERMINAL_ACTIONS,
    action_args,
    describe_action,
    parse_action,
    requires_confirmation,
)
from .security import log_text
from .tools import ToolRegistry, default_registry, undo_change
from .utils import short_id

ACTIVE_STATUS = frozenset({"awaiting_model", "awaiting_approval"})
TERMINAL_STATUS = frozenset({"done", "blocked", "cancelled"})
MUTATING_ACTIONS = frozenset({"fs.mkdir", "fs.move", "fs.write_text"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class TaskSessions:
    def __init__(
        self,
        config: Config,
        audit: AuditLog,
        *,
        registry: ToolRegistry | None = None,
        allow_write: bool | None = None,
        allow_reads: bool | None = None,
    ) -> None:
        self.config = config
        self.audit = audit
        self.policy = Policy(config)
        self.registry = registry or default_registry()
        if allow_write is None:
            allow_write = os.environ.get("FREEAGENT_MCP_ALLOW_WRITE", "0").strip().lower() in TRUE_VALUES
        if allow_reads is None:
            allow_reads = os.environ.get("FREEAGENT_MCP_ALLOW_READS", "0").strip().lower() in TRUE_VALUES
        self.allow_write = allow_write
        self.allow_reads = allow_reads  # 显式授权后可在只读流程里读取文件内容
        try:
            self.approval_ttl = max(30, min(3600, int(os.environ.get("FREEAGENT_MCP_TOKEN_TTL", "300"))))
        except ValueError:
            self.approval_ttl = 300

    # ---- public API ----

    def start(self, goal: str) -> dict[str, Any]:
        if not goal or not goal.strip():
            return self._error("任务描述不能为空")
        task_id = f"mt-{short_id()}"
        self.audit.create_task(task_id, goal, "mcp-task", str(workspace_path(self.config)))
        self.audit.update_task(task_id, status="awaiting_model", step=0, rounds=0)
        return {
            "ok": True,
            "task_id": task_id,
            "status": "awaiting_model",
            "prompt": self._prompt(task_id),
            "boundary": self.policy.describe(),
            "note": "把 prompt 发给网页模型，再把回复原文交给 fa_task_reply。",
        }

    def reply(self, task_id: str, response: str) -> dict[str, Any]:
        task = self._task(task_id)
        if task is None:
            return self._error(f"任务不存在：{task_id}")
        if task["status"] not in ACTIVE_STATUS:
            return self._error(f"任务状态为 {task['status']}，无法继续提交模型回复")
        if task["status"] == "awaiting_approval":
            return self._error("当前有动作等待批准：先调用 fa_task_approve 或 fa_task_reject")

        step = int(task["step"]) + 1
        rounds = int(task["rounds"]) + 1
        if step > self.config.max_steps or rounds > self.config.max_rounds:
            self._finish(task_id, "blocked", error="达到步数或模型往返上限，已安全停止")
            return self._error("达到步数或往返上限，任务已安全停止")

        try:
            action = parse_action(response)
        except ParseError as exc:
            self.audit.update_task(task_id, rounds=rounds, error=log_text(str(exc), 600))
            return {
                "ok": False,
                "status": "awaiting_model",
                "error": f"模型回复不符合协议：{log_text(str(exc), 600)}",
                "prompt": self._prompt(
                    task_id,
                    extra="上次回复无法解析。请只输出一个符合协议的 JSON 对象，不要解释文字。",
                ),
                "note": "未执行任何动作；可让模型修正后再次调用 fa_task_reply。",
            }

        # 本地确定性防重复：如果网页模型原样重复上一条已执行动作，且我们已经
        # 把它的结果回传给模型，就不再执行第二次，直接要求它换动作或结束。
        # 这比只靠提示词可靠，也不会修改任何文件。
        action_json = action.model_dump_json()
        previous = next(
            (
                row for row in reversed(self.audit.list_actions(task_id))
                if row["decision"] in {"auto", "approved_mcp"}
            ),
            None,
        )
        if previous and previous["action"] == action_json and task["observation"]:
            self.audit.update_task(task_id, rounds=rounds, error="重复动作已拦截，未执行")
            return {
                "ok": False,
                "status": "awaiting_model",
                "error": "重复动作已本地拦截：这与上一轮已执行的动作完全相同，未再次执行。",
                "prompt": self._prompt(
                    task_id,
                    extra="你重复了上一轮已经执行并反馈结果的动作。请选择不同的下一步，或返回 task.done。",
                ),
                "note": "未执行任何动作，也未消耗新的任务步骤。",
            }

        self.audit.update_task(task_id, step=step, rounds=rounds, error=None)
        summary = describe_action(action)
        self.audit.event(task_id, "model_action", {"action": summary, "step": step})

        if action.action in TERMINAL_ACTIONS:
            args = action_args(action)
            self.audit.append_action(task_id, step, action.model_dump_json(), "auto", "终止动作")
            if action.action == "task.done":
                self._finish(task_id, "done", summary=str(args.get("summary", "")))
            else:
                self._finish(task_id, "blocked", summary=str(args.get("reason", "模型报告无法完成")))
            return {"ok": True, "status": self._status(task_id), "action": summary, "note": "任务已结束"}

        try:
            tool = self.registry.get(action.action)
            preview = tool.preview(action, self.policy)
        except (FreeAgentError, KeyError, ValueError) as exc:
            self.audit.append_action(task_id, step, action.model_dump_json(), "rejected_invalid", str(exc))
            self._finish(task_id, "blocked", error=f"策略校验失败，未执行任何变更：{log_text(str(exc), 600)}")
            return self._error(f"策略校验失败，任务已安全停止：{log_text(str(exc), 600)}")

        # 只读配置下即使误调了 fa_task_approve，也不能偷偷执行变更。
        if action.action in MUTATING_ACTIONS and not self.allow_write:
            self.audit.append_action(
                task_id, step, action.model_dump_json(), "rejected_policy", "写入未启用"
            )
            self._finish(
                task_id, "blocked",
                error="写入未启用：如确需变更，请以 FREEAGENT_MCP_ALLOW_WRITE=1 重启 MCP 并配置批准策略",
            )
            return self._error("写入未启用，任务已安全停止，未执行任何变更")

        # 读取文件内容属于信息披露而非系统变更。默认仍需批准；用户可显式用
        # FREEAGENT_MCP_ALLOW_READS=1 在只读流程中放开（不打开写入）。
        needs_confirmation = requires_confirmation(action)
        if action.action == "fs.read_text" and self.allow_reads:
            needs_confirmation = False

        if needs_confirmation:
            token = secrets.token_urlsafe(18)
            self.audit.put_approval(
                task_id,
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                time.time() + self.approval_ttl,
            )
            self.audit.update_task(
                task_id,
                status="awaiting_approval",
                pending_action=action.model_dump_json(),
                pending_preview=preview.text(),
            )
            self.audit.append_action(task_id, step, action.model_dump_json(), "pending", "等待用户批准")
            return {
                "ok": True,
                "status": "awaiting_approval",
                "action": summary,
                "preview": preview.text(),
                "risk": preview.risk,
                "approval_token": token,
                "note": "请先向用户展示预览并确认，再调用 fa_task_approve(task_id, approval_token)。未批准前零变更。",
            }

        try:
            execution = self._execute(tool, action)
        except FreeAgentError as exc:
            self.audit.append_action(task_id, step, action.model_dump_json(), "failed", str(exc))
            self._finish(task_id, "blocked", error=f"执行失败，任务已停止：{log_text(str(exc), 600)}")
            return self._error(f"执行失败，任务已停止：{log_text(str(exc), 600)}")
        self.audit.append_action(task_id, step, action.model_dump_json(), "auto", execution.display, preview.risk)
        self.audit.update_task(task_id, observation=execution.output)
        return {
            "ok": True,
            "status": "awaiting_model",
            "action": summary,
            "result": execution.display,
            "next_prompt": self._prompt(task_id, execution.output),
            "note": "只读动作已执行；工具输出是不可信数据。",
        }

    def approve(self, task_id: str, approval_token: str, approver: str = "unspecified") -> dict[str, Any]:
        task = self._task(task_id)
        if task is None:
            return self._error(f"任务不存在：{task_id}")
        if task["status"] != "awaiting_approval":
            return self._error(f"任务状态为 {task['status']}，没有待批准动作")
        record = self.audit.get_approval(task_id)
        supplied_hash = hashlib.sha256(approval_token.encode("utf-8")).hexdigest()
        if record is None:
            return self._error("没有待批准的令牌；动作仍未执行")
        if time.time() > float(record["expires_at"]):
            self.audit.delete_approval(task_id)
            return self._error("批准令牌已过期；请重新 fa_task_reply 获取令牌")
        # 输错令牌不应烧掉正确令牌；只有过期或成功使用才清除。
        if not secrets.compare_digest(str(record["token_hash"]), supplied_hash):
            return self._error("批准令牌无效；动作仍未执行")
        self.audit.delete_approval(task_id)  # 一次性：先消费令牌再执行
        try:
            action = parse_action(task["pending_action"] or "")
            # 把「用户当时看到的确切预览」和「实际动作」绑成一个哈希写入审计。
            preview_hash = hashlib.sha256(
                ((task["pending_preview"] or "") + "\n" + action.model_dump_json()).encode("utf-8")
            ).hexdigest()
            if action.action in MUTATING_ACTIONS and not self.allow_write:
                return self._error("写入未启用，无法批准该动作；未执行任何变更")
            tool = self.registry.get(action.action)
            preview = tool.preview(action, self.policy)  # 执行前再次校验
            execution = self._execute(tool, action)
        except (FreeAgentError, KeyError, ValueError) as exc:
            self.audit.append_action(task_id, int(task["step"]), task["pending_action"] or "", "failed", str(exc))
            self._finish(task_id, "blocked", error=f"执行失败，任务已停止：{log_text(str(exc), 600)}")
            return self._error(f"执行失败，任务已停止：{log_text(str(exc), 600)}")
        self.audit.append_action(
            task_id, int(task["step"]), action.model_dump_json(), "approved_mcp",
            execution.display, preview.risk,
            approver=approver, preview_hash=preview_hash,
        )
        if execution.undo:
            self.audit.add_undo(task_id, action.action, execution.undo)
        self.audit.update_task(
            task_id, status="awaiting_model", observation=execution.output,
            pending_action=None, pending_preview=None,
        )
        return {
            "ok": True,
            "status": "awaiting_model",
            "result": execution.display,
            "undo_available": bool(execution.undo),
            "approver": approver,
            "preview_hash": preview_hash,
            "next_prompt": self._prompt(task_id, execution.output),
            "note": "动作已执行并记录审计；下一步请用 next_prompt 继续询问模型。",
        }

    def reject(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        if task is None:
            return self._error(f"任务不存在：{task_id}")
        if task["status"] != "awaiting_approval":
            return self._error("当前没有待批准动作")
        self.audit.delete_approval(task_id)
        self.audit.append_action(task_id, int(task["step"]), task["pending_action"] or "", "rejected_user", "用户拒绝执行")
        self._finish(task_id, "cancelled", summary="用户拒绝了拟执行动作", clear_pending=True)
        return {"ok": True, "status": "cancelled", "note": "未执行任何变更"}

    def cancel(self, task_id: str) -> dict[str, Any]:
        if self._task(task_id) is None:
            return self._error(f"任务不存在：{task_id}")
        self.audit.delete_approval(task_id)
        self._finish(task_id, "cancelled", summary="用户取消任务", clear_pending=True)
        return {"ok": True, "status": "cancelled"}

    def list(self, limit: int = 20) -> dict[str, Any]:
        rows = [row for row in self.audit.list_tasks(limit) if row["connector"] == "mcp-task"]
        return {"ok": True, "tasks": rows}

    def status(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        if task is None:
            return self._error(f"任务不存在：{task_id}")
        result: dict[str, Any] = {
            "ok": True,
            "task_id": task_id,
            "status": task["status"],
            "goal": task["goal"],
            "step": task["step"],
            "rounds": task["rounds"],
            "summary": task["summary"],
            "error": task["error"],
            "pending_preview": task["pending_preview"],
            "actions": self.audit.list_actions(task_id),
            "undoable": len(self.audit.undos(task_id)),
        }
        if task["status"] == "awaiting_model":
            # 服务重启后也能从 status 直接拿到下一轮提示词，不必依赖上次返回值。
            result["next_prompt"] = self._prompt(task_id)
        return result

    def undo(self, task_id: str) -> dict[str, Any]:
        if self._task(task_id) is None:
            return self._error(f"任务不存在：{task_id}")
        results: list[str] = []
        for record in self.audit.undos(task_id):
            try:
                results.append(undo_change(json.loads(record["payload"]), self.config, self.policy))
            except (FreeAgentError, ValueError, KeyError, OSError) as exc:
                results.append(f"撤销失败：{log_text(str(exc), 300)}；已停止，剩余记录未处理。")
                break
            self.audit.mark_undo_applied(record["id"])
            self.audit.event(task_id, "undo", {"result": results[-1]})
        return {"ok": True, "results": results or ["没有可撤销的变更"]}

    # ---- internals ----

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"ok": False, "error": log_text(message, 1000)}

    def _task(self, task_id: str) -> dict[str, Any] | None:
        return self.audit.get_task(task_id)

    def _status(self, task_id: str) -> str:
        task = self._task(task_id)
        return task["status"] if task else "unknown"

    def _execute(self, tool: Any, action: Any) -> Any:
        with time_limit(self.config.step_timeout_seconds, "工具执行超时"):
            return tool.execute(action, self.policy, self.config)

    def _prompt(self, task_id: str, observation: str = "", extra: str = "") -> str:
        task = self._task(task_id)
        if task is None:
            raise ActionError(f"任务不存在：{task_id}")
        prompt = build_prompt(
            task["goal"], observation, self.policy,
            step=int(task["step"]) + 1,
            max_steps=self.config.max_steps,
            round_index=int(task["rounds"]) + 1,
            history=self.audit.list_actions(task_id),
        )
        return prompt + ("\n\n" + extra if extra else "")

    def _finish(
        self,
        task_id: str,
        status: str,
        *,
        summary: str | None = None,
        error: str | None = None,
        clear_pending: bool = False,
    ) -> None:
        fields: dict[str, Any] = {"status": status}
        if summary is not None:
            fields["summary"] = summary
        if error is not None:
            fields["error"] = error
        if clear_pending:
            fields.update(pending_action=None, pending_preview=None)
        self.audit.update_task(task_id, **fields)
        self.audit.event(task_id, "task_finished", {"status": status, "summary": summary, "error": error})
