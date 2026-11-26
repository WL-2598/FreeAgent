"""本地配置与状态目录。

配置/审计文件只保存在本机；默认目录权限为 0700，配置文件为 0600。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .errors import ConfigError
from .utils import atomic_write_text

# 默认拒绝的名称/后缀；用户只能追加规则，不能移除这些默认值。
DEFAULT_DENY_NAMES = frozenset(
    {
        ".env", ".env.local", ".npmrc", ".pypirc", ".netrc", ".htpasswd",
        "id_rsa", "id_ed25519", "id_ecdsa", "credentials", "credentials.json",
        "authorized_keys", "shadow", "passwd",
    }
)
DEFAULT_DENY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workspace: str = Field(default_factory=lambda: str(Path.home() / "agent-workspace"))
    extra_roots: list[str] = Field(default_factory=list, description="用户显式授权的额外目录")
    allowed_domains: list[str] = Field(
        default_factory=lambda: ["example.com", "example.org", "example.net"],
        description="browser.open 允许访问的域名（需用户按需修改）",
    )
    default_connector: str = "manual"
    max_steps: int = Field(default=8, ge=1, le=50)
    max_rounds: int = Field(default=12, ge=1, le=100)
    max_file_bytes: int = Field(default=256 * 1024, gt=0, le=8 * 1024 * 1024)
    max_output_chars: int = Field(default=6000, gt=0, le=100_000)
    step_timeout_seconds: int = Field(default=30, ge=1, le=600)
    model_timeout_seconds: int = Field(default=0, ge=0, description="0 表示不超时（手工粘贴需要）")
    parse_retries: int = Field(default=2, ge=0, le=5)
    audit_retention_days: int = Field(default=90, ge=1, le=3650)
    # 默认关闭：自动清理会删除本地审计，必须由用户显式开启。
    audit_auto_purge: bool = Field(default=False, description="启动时自动清理超过保留期的审计（需显式开启）")
    deny_names: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_DENY_NAMES),
        description="额外拒绝的文件名（默认规则永远生效，不可移除）",
    )
    deny_suffixes: list[str] = Field(
        default_factory=lambda: sorted(DEFAULT_DENY_SUFFIXES),
        description="额外拒绝的后缀（默认规则永远生效，不可移除）",
    )
    workspace_rules: dict[str, dict[str, list[str]]] = Field(
        default_factory=dict,
        description="按工作区绝对路径追加 deny_names/deny_suffixes，只能更严格",
    )


class Layout:
    """基于 FREEAGENT_HOME（默认 ~/.freeagent）的目录布局。"""

    def __init__(self, home: str | os.PathLike[str] | None = None) -> None:
        self.home = Path(home or os.environ.get("FREEAGENT_HOME", Path.home() / ".freeagent")).expanduser()
        self.config_path = self.home / "config.json"
        self.db_path = self.home / "state.db"
        self.audit_dir = self.home / "audit"
        self.cancel_dir = self.home / "cancel"

    def ensure(self) -> None:
        try:
            for directory in (self.home, self.audit_dir, self.cancel_dir):
                directory.mkdir(parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
        except OSError as exc:
            raise ConfigError(f"无法创建 FreeAgent 本地目录 {self.home}: {exc}") from exc

    def cancel_flag(self, task_id: str) -> Path:
        return self.cancel_dir / f"{task_id}.cancel"


def default_layout() -> Layout:
    return Layout()


def load_config(layout: Layout | None = None) -> Config:
    layout = layout or default_layout()
    if not layout.config_path.exists():
        cfg = Config()
    else:
        try:
            raw: Any = json.loads(layout.config_path.read_text(encoding="utf-8"))
            cfg = Config.model_validate(raw)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"配置文件无效：{layout.config_path}（{exc}）") from exc
    # 环境变量适合 CI/沙箱/测试；显式传入的优先级最高。
    env_workspace = os.environ.get("FREEAGENT_WORKSPACE")
    if env_workspace:
        cfg.workspace = env_workspace
    return cfg


def save_config(config: Config, layout: Layout | None = None) -> Path:
    layout = layout or default_layout()
    layout.ensure()
    atomic_write_text(
        layout.config_path,
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    return layout.config_path


def setup(layout: Layout | None = None) -> tuple[Config, Path]:
    """首次运行：创建工作区与本地状态目录，返回配置和配置文件路径。"""
    layout = layout or default_layout()
    layout.ensure()
    cfg = load_config(layout)
    workspace = Path(cfg.workspace).expanduser()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建工作区 {workspace}: {exc}") from exc
    if not layout.config_path.exists():
        save_config(cfg, layout)
    return cfg, layout.config_path


def workspace_path(config: Config) -> Path:
    return Path(config.workspace).expanduser()
