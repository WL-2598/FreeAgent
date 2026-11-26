"""本地审计：SQLite 保存任务状态/动作/撤销记录，JSONL 保存可读日志。

日志默认只在 ~/.freeagent 下，权限 0700；写入前统一脱敏 + 截断。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Layout, default_layout
from .security import log_text
from .utils import atomic_write_text, utcnow

TASK_FIELDS = frozenset(
    {
        "status", "step", "rounds", "pending_action", "pending_preview",
        "observation", "summary", "error", "connector", "updated_at",
    }
)


class AuditLog:
    def __init__(self, layout: Layout | None = None) -> None:
        self.layout = layout or default_layout()
        self.layout.ensure()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.layout.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    connector TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    step INTEGER NOT NULL DEFAULT 0,
                    rounds INTEGER NOT NULL DEFAULT 0,
                    pending_action TEXT,
                    pending_preview TEXT,
                    observation TEXT,
                    summary TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    result TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS actions_task ON actions(task_id, seq);
                CREATE TABLE IF NOT EXISTS undo_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS undo_task ON undo_records(task_id, applied, id);
                CREATE TABLE IF NOT EXISTS approval_tokens (
                    task_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    # ---- tasks ----

    def create_task(self, task_id: str, goal: str, connector: str, workspace: str) -> None:
        now = utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, goal, status, connector, workspace, created_at, updated_at) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?)",
                (task_id, log_text(goal, 4000), connector, workspace, now, now),
            )
        self.event(task_id, "task_created", {"goal": goal, "connector": connector})

    def update_task(self, task_id: str, **fields: Any) -> None:
        unknown = set(fields) - TASK_FIELDS
        if unknown:
            raise ValueError(f"未知任务字段：{sorted(unknown)}")
        if not fields:
            return
        fields.setdefault("updated_at", utcnow())
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=?",
                (*[self._prepare(key, value) for key, value in fields.items()], task_id),
            )

    @staticmethod
    def _prepare(key: str, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            # 结构化状态字段要完整保存，避免动作 JSON 被中途截断后无法恢复。
            limit = 100_000 if key in {"pending_action", "pending_preview", "observation"} else 20_000
            return log_text(value, limit)
        return value

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, goal, status, connector, created_at, updated_at, summary, error "
                "FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- actions / undo ----

    def append_action(
        self,
        task_id: str,
        seq: int,
        action: str,
        decision: str,
        result: str,
        risk: str = "低",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO actions (task_id, seq, action, decision, result, risk, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, seq, log_text(action, 1200), decision,
                    log_text(result, 2000), risk, utcnow(),
                ),
            )
        self.event(task_id, "action", {"seq": seq, "action": action, "decision": decision, "result": result, "risk": risk})

    def list_actions(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, action, decision, result, risk, created_at FROM actions "
                "WHERE task_id=? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_undo(self, task_id: str, action: str, payload: dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO undo_records (task_id, action, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, log_text(action, 200), json.dumps(payload, ensure_ascii=False), utcnow()),
            )
            return int(cursor.lastrowid)

    def undos(self, task_id: str, *, applied: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, action, payload, applied FROM undo_records "
                "WHERE task_id=? AND applied=? ORDER BY id DESC",
                (task_id, int(applied)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_undo_applied(self, undo_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE undo_records SET applied=1 WHERE id=?", (undo_id,))

    # ---- approval tokens (只存哈希，重启后仍可校验且不会泄露明文令牌) ----

    def put_approval(self, task_id: str, token_hash: str, expires_at: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO approval_tokens (task_id, token_hash, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, token_hash, expires_at, utcnow()),
            )

    def get_approval(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT token_hash, expires_at FROM approval_tokens WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_approval(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM approval_tokens WHERE task_id=?", (task_id,))

    # ---- readable log ----

    def event(self, task_id: str, event: str, data: dict[str, Any] | None = None) -> None:
        path: Path = self.layout.audit_dir / f"{task_id}.jsonl"
        line = json.dumps(
            {"ts": utcnow(), "task_id": task_id, "event": event, "data": data or {}},
            ensure_ascii=False,
        )
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(log_text(line, 8000) + "\n")
        except OSError:
            # 审计日志写入失败不应破坏主流程，但状态库中仍有记录。
            pass

    def purge_old(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._connect() as conn:
            with conn:
                conn.execute("DELETE FROM actions WHERE task_id IN (SELECT id FROM tasks WHERE created_at < ?)", (cutoff,))
                conn.execute("DELETE FROM undo_records WHERE task_id IN (SELECT id FROM tasks WHERE created_at < ?)", (cutoff,))
                cursor = conn.execute("DELETE FROM tasks WHERE created_at < ?", (cutoff,))
                removed = cursor.rowcount
        for path in self.layout.audit_dir.glob("*.jsonl"):
            if path.stat().st_mtime < (datetime.now(timezone.utc) - timedelta(days=days)).timestamp():
                path.unlink(missing_ok=True)
        return int(removed)
