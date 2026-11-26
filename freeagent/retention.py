"""本地审计保留期辅助逻辑。

自动清理是**显式 opt-in**：默认不删除任何数据；关闭时这个函数是空操作。
"""

from __future__ import annotations

from .audit import AuditLog
from .config import Layout, load_config


def maybe_auto_purge(layout: Layout | None = None) -> int:
    """若用户在配置中显式开启 audit_auto_purge，则清理过期审计并返回任务数。

    配置缺失/损坏或审计库异常时静默跳过，由 doctor 输出具体原因；
    启动流程不应因为清理失败而中断。
    """
    from .config import default_layout

    target = layout or default_layout()
    if not target.config_path.exists():
        return 0
    try:
        cfg = load_config(target)
        if not cfg.audit_auto_purge:
            return 0
        return AuditLog(target).purge_old(cfg.audit_retention_days)
    except Exception:
        return 0
