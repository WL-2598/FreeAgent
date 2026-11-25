"""任务编排器：单步闭环 + 严格校验 + 用户批准 + 安全停止。

流程：模型给一个动作 → 本地校验 → 预览/确认 → 执行 → 反馈 → 下一个动作。
任何一环失败即停止当前任务，不自动降级、不猜测、不重试更高权限操作。
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from .audit import AuditLog
from .config import Config, Layout, default_layout
from .connectors.base import AskContext, Connector
from .errors import ActionError, Cancelled, ConnectorError, FreeAgentError, NeedsApproval, PolicyError, ToolError
from .planner import build_prompt
from .policy import Policy
from .protocol import (
    TERMINAL_ACTIONS,
    Action,
    action_args,
    describe_action,
    parse_action,
    validate_action,
)
from .security import log_text
from .tools import ToolRegistry, default_registry, undo_change
from .utils import short_id

TERMINAL_STATUS = frozenset({"done", "blocked", "cancelled"})


@contextmanager
def time_limit(seconds: int, message: str) -> Iterator[None]:
    """Linux 主线程超时保护；0 表示不设置超时。"""
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: Any) -> None:
        raise ConnectorError(message)

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class UI(Protocol):
    def say(self, message: str) -> None: ...

    def confirm(self, action: Action, preview_text: str) -> bool: ...


class ConsoleUI:
    """终端交互：非 TTY 时不自动批准，而是暂停任务等待 `freeagent approve`。"""

    def __init__(self, *, force_interactive: bool = False) -> None:
        self.force_interactive = force_interactive

    @property
    def interactive(self) -> bool:
        return self.force_interactive or bool(sys.stdin.isatty())

    def say(self, message: str) -> None:
        print(message, flush=True)

    def confirm(self, action: Action, preview_text: str) -> bool:
        if not self.interactive:
            raise NeedsApproval("非交互终端：暂停等待显式批准")
        self.say("\n拟执行动作：")
        self.say(preview_text)
        answer = input("是否执行此动作？[y/N] ").strip().lower()
        return answer in {"y", "yes"}


class AutoApproveUI:
    """仅供演示/测试：显式启用后自动批准所有动作。默认绝不使用。"""

    def say(self, message: str) -> None:
        print(message, flush=True)

    def confirm(self, action: Action, preview_text: str) -> bool:
        return True


class RecordingUI:
    """测试用 UI，记录全部输出。"""

    def __init__(self, *, approve: bool = True) -> None:
        self.messages: list[str] = []
        self.approve = approve

    def say(self, message: str) -> None:
        self.messages.append(message)

    def confirm(self, action: Action, preview_text: str) -> bool:
        if not self.approve:
            return False
        self.messages.append(f"[已批准] {describe_action(action)}")
        return True


class TaskRunner:
    def __init__(
        self,
        config: Config,
        *,
        layout: Layout | None = None,
        audit: AuditLog | None = None,
        registry: ToolRegistry | None = None,
        ui: UI | None = None,
    ) -> None:
        self.config = config
        self.layout = layout or default_layout()
        self.audit = audit or AuditLog(self.layout)
        self.policy = Policy(config)
        self.registry = registry or default_registry()
        self.ui: UI = ui or ConsoleUI()

    # ---- public API ----

    def run(self, goal: str, connector: Connector, *, task_id: str | None = None) -> dict[str, Any]:
        if not goal or not goal.strip():
            raise ActionError("任务描述不能为空")
        task_id = task_id or short_id()
        workspace = str(Path(self.config.workspace).expanduser())
        self.audit.create_task(task_id, goal, connector.id, workspace)
        self.ui.say(f"任务 {task_id} 已创建。连接器：{connector.label}（id={connector.id}）")
        self.ui.say("默认 dry-run：只读动作自动执行；读取内容/打开网页/写入变更均需明确确认。")
        self.ui.say(f"本地边界：\n{self.policy.describe()}")
        try:
            return self._loop(task_id, connector)
        except KeyboardInterrupt:
            cancel_task(task_id, self.layout, self.audit)
            self.ui.say("\n检测到 Ctrl-C：任务已安全中止。")
            return self.audit.get_task(task_id) or {}

    def resume(self, task_id: str, approved: bool, connector: Connector) -> dict[str, Any]:
        task = self.audit.get_task(task_id)
        if task is None:
            raise ActionError(f"任务不存在：{task_id}")
        if task["status"] != "awaiting_approval":
            raise ActionError(f"任务 {task_id} 当前状态为 {task['status']}，无需批准")
        if not approved:
            self.audit.append_action(task_id, int(task["step"]), task["pending_action"] or "", "rejected_user", "用户拒绝执行")
            self._finish(task_id, "cancelled", summary="用户拒绝了拟执行动作", clear_pending=True)
            return self.audit.get_task(task_id) or {}
        try:
            action = validate_action(json.loads(task["pending_action"] or ""))
        except (ValueError, FreeAgentError) as exc:
            self._finish(task_id, "blocked", error=f"待批准动作损坏：{log_text(str(exc), 300)}", clear_pending=True)
            return self.audit.get_task(task_id) or {}
        tool = self.registry.get(action.action)
        preview = tool.preview(action, self.policy)
        self.audit.update_task(task_id, pending_action=None, pending_preview=None)
        self._execute_and_record(task_id, action, tool, preview, int(task["step"]), "approved")
        return self._loop(task_id, connector)

    def undo(self, task_id: str) -> list[str]:
        task = self.audit.get_task(task_id)
        if task is None:
            raise ActionError(f"任务不存在：{task_id}")
        results: list[str] = []
        for record in self.audit.undos(task_id):
            try:
                payload = json.loads(record["payload"])
                results.append(undo_change(payload, self.config, self.policy))
            except (ValueError, KeyError, FreeAgentError, OSError) as exc:
                results.append(f"撤销失败（记录 #{record['id']}）：{log_text(str(exc), 300)}；已停止，剩余记录未处理。")
                break
            self.audit.mark_undo_applied(record["id"])
            self.audit.event(task_id, "undo", {"id": record["id"], "result": results[-1]})
            self.ui.say(results[-1])
        if not results:
            self.ui.say("该任务没有可撤销的变更。")
        return results

    def status(self, task_id: str) -> dict[str, Any]:
        task = self.audit.get_task(task_id)
        if task is None:
            raise ActionError(f"任务不存在：{task_id}")
        return task

    # ---- internals ----

    def _check_cancel(self, task_id: str) -> None:
        flag = self.layout.cancel_flag(task_id)
        task = self.audit.get_task(task_id)
        if flag.exists() or (task and task["status"] == "cancelled"):
            flag.unlink(missing_ok=True)
            raise Cancelled("用户取消了任务")

    def _ask(self, prompt: str, task_id: str, step: int, round_index: int, connector: Connector) -> str:
        return self._timed(
            self.config.model_timeout_seconds,
            f"模型响应超时（{self.config.model_timeout_seconds}s）",
            connector.ask,
            prompt,
            AskContext(task_id=task_id, step=step, round_index=round_index),
        )

    @staticmethod
    def _timed(seconds: int, message: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        with time_limit(seconds, message):
            return func(*args, **kwargs)

    def _loop(self, task_id: str, connector: Connector) -> dict[str, Any]:
        while True:
            task = self.audit.get_task(task_id)
            if task is None or task["status"] in TERMINAL_STATUS:
                return task or {}
            try:
                self._check_cancel(task_id)
            except Cancelled:
                self._finish(task_id, "cancelled", summary="用户取消了任务", clear_pending=True)
                return self.audit.get_task(task_id) or {}

            step = int(task["step"]) + 1
            rounds = int(task["rounds"])
            if step > self.config.max_steps:
                self._finish(task_id, "blocked", error=f"达到步数上限（{self.config.max_steps}），已安全停止")
                return self.audit.get_task(task_id) or {}
            if rounds >= self.config.max_rounds:
                self._finish(task_id, "blocked", error=f"达到模型往返上限（{self.config.max_rounds}），已安全停止")
                return self.audit.get_task(task_id) or {}

            prompt = build_prompt(
                task["goal"], task["observation"] or "", self.policy,
                step=step, max_steps=self.config.max_steps, round_index=rounds + 1,
            )
            try:
                raw = self._ask(prompt, task_id, step, rounds + 1, connector)
                action, used_rounds = self._parse_with_retries(raw, prompt, task_id, step, rounds, connector)
            except ConnectorError as exc:
                self._finish(task_id, "blocked", error=f"连接器异常：{log_text(str(exc), 600)}")
                return self.audit.get_task(task_id) or {}
            except Cancelled:
                self._finish(task_id, "cancelled", summary="用户取消了任务", clear_pending=True)
                return self.audit.get_task(task_id) or {}

            self.audit.update_task(task_id, step=step, rounds=used_rounds)
            # 模型往返期间用户可能已取消：执行前必须再次检查，避免竞态。
            try:
                self._check_cancel(task_id)
            except Cancelled:
                self._finish(task_id, "cancelled", summary="用户取消了任务", clear_pending=True)
                return self.audit.get_task(task_id) or {}
            self.ui.say(f"\n[步骤 {step}/{self.config.max_steps}] 模型提出：{describe_action(action)}")

            if action.action in TERMINAL_ACTIONS:
                self.audit.append_action(task_id, step, action.model_dump_json(), "auto", "终止动作")
                args = action_args(action)
                if action.action == "task.done":
                    self._finish(task_id, "done", summary=str(args.get("summary", "")))
                else:
                    self._finish(task_id, "blocked", summary=str(args.get("reason", "模型报告无法完成")))
                return self.audit.get_task(task_id) or {}

            tool = self.registry.get(action.action)
            try:
                preview = tool.preview(action, self.policy)
            except (FreeAgentError, ValueError) as exc:
                self.audit.append_action(task_id, step, action.model_dump_json(), "rejected_invalid", str(exc))
                self._finish(task_id, "blocked", error=f"策略校验失败，未执行任何变更：{log_text(str(exc), 600)}")
                return self.audit.get_task(task_id) or {}

            self.ui.say("动作预览：")
            self.ui.say(preview.text())
            self.ui.say(f"风险：{preview.risk}")

            from .protocol import requires_confirmation

            decision = "auto"
            if requires_confirmation(action):
                try:
                    approved = self.ui.confirm(action, preview.text())
                except NeedsApproval:
                    self.audit.append_action(task_id, step, action.model_dump_json(), "pending", "等待用户批准")
                    self.audit.update_task(
                        task_id,
                        status="awaiting_approval",
                        pending_action=action.model_dump_json(),
                        pending_preview=preview.text(),
                    )
                    self.ui.say(f"\n任务已暂停，等待明确批准：freeagent approve {task_id}")
                    return self.audit.get_task(task_id) or {}
                decision = "approved" if approved else "rejected_user"
                if not approved:
                    self.audit.append_action(task_id, step, action.model_dump_json(), decision, "用户拒绝执行")
                    self._finish(task_id, "cancelled", summary="用户拒绝了拟执行动作")
                    return self.audit.get_task(task_id) or {}

            try:
                self._execute_and_record(task_id, action, tool, preview, step, decision)
            except FreeAgentError as exc:
                self._finish(task_id, "blocked", error=f"执行失败，已停止：{log_text(str(exc), 600)}")
                return self.audit.get_task(task_id) or {}

    def _parse_with_retries(
        self, raw: str, prompt: str, task_id: str, step: int, rounds: int, connector: Connector
    ) -> tuple[Action, int]:
        from .errors import ParseError

        used = rounds
        response = raw
        for attempt in range(self.config.parse_retries + 1):
            used += 1
            try:
                return parse_action(response), used
            except ParseError as exc:
                if attempt >= self.config.parse_retries:
                    raise ConnectorError(
                        f"模型连续 {attempt + 1} 次未返回合法 JSON 动作（{exc}）。"
                        f"原文片段：{log_text(raw, 500)}。任务已安全停止。"
                    ) from exc
                correction = (
                    prompt
                    + f"\n\n上一次回复无法解析：{exc}。请只输出一个符合协议的 JSON 对象，不要解释文字。"
                )
                response = self._ask(correction, task_id, step, used + 1, connector)
        raise AssertionError("unreachable")

    def _execute_and_record(
        self, task_id: str, action: Action, tool: Any, preview: Any, step: int, decision: str
    ) -> None:
        try:
            execution = self._timed(
                self.config.step_timeout_seconds,
                f"工具执行超时（{self.config.step_timeout_seconds}s）",
                tool.execute,
                action,
                self.policy,
                self.config,
            )
        except FreeAgentError as exc:
            self.audit.append_action(task_id, step, action.model_dump_json(), "failed", str(exc))
            raise
        self.audit.append_action(
            task_id, step, action.model_dump_json(), decision,
            execution.display or execution.output, preview.risk,
        )
        if execution.undo:
            self.audit.add_undo(task_id, action.action, execution.undo)
        self.audit.update_task(task_id, observation=execution.output, pending_action=None, pending_preview=None)
        self.ui.say(f"执行结果：{execution.display or '成功'}")

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
        self.layout.cancel_flag(task_id).unlink(missing_ok=True)
        message = f"\n任务 {status}：{summary or error or ''}"
        self.ui.say(message)
        if status in {"done", "blocked", "cancelled"} and error:
            self.ui.say(f"错误：{error}")


def cancel_task(task_id: str, layout: Layout | None = None, audit: AuditLog | None = None) -> None:
    """外部中止：写标志文件（运行中的任务会在下一步前停止），并更新状态。"""
    layout = layout or default_layout()
    layout.ensure()
    layout.cancel_flag(task_id).write_text("cancelled\n", encoding="utf-8")
    log = audit or AuditLog(layout)
    task = log.get_task(task_id)
    if task and task["status"] not in TERMINAL_STATUS:
        log.update_task(task_id, status="cancelled", error="用户取消", pending_action=None, pending_preview=None)
        log.event(task_id, "cancelled", {})
