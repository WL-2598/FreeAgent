"""审计保留期：默认只预览/不自动删除，显式开启后才清理。"""

from __future__ import annotations

import sqlite3

from freeagent.audit import AuditLog
from freeagent.config import save_config
from freeagent.retention import maybe_auto_purge

from .conftest import Env


def _old_task(env: Env, task_id: str) -> None:
    audit = AuditLog(env.layout)
    audit.create_task(task_id, "old goal", "mcp", str(env.workspace))
    audit.append_action(task_id, 1, '{"action":"fs.list"}', "auto", "ok")
    with sqlite3.connect(env.layout.db_path) as conn:
        conn.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )


def test_purge_preview_counts_without_deleting(env: Env) -> None:
    _old_task(env, "preview-task")
    audit = AuditLog(env.layout)
    preview = audit.purge_preview(1)
    assert preview["tasks"] == 1
    assert preview["actions"] == 1
    assert audit.get_task("preview-task") is not None  # 预览绝不删除


def test_auto_purge_is_opt_in(env: Env) -> None:
    _old_task(env, "default-off")
    assert maybe_auto_purge(env.layout) == 0
    assert AuditLog(env.layout).get_task("default-off") is not None

    cfg = env.config.model_copy(update={"audit_auto_purge": True})
    save_config(cfg, env.layout)
    assert maybe_auto_purge(env.layout) == 1
    assert AuditLog(env.layout).get_task("default-off") is None
