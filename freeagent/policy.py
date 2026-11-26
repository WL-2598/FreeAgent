"""本地权限策略：路径沙箱、敏感文件过滤与域名白名单。

这是安全边界的核心。所有工具在执行前（以及执行前再次）都必须经过这里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .config import DEFAULT_DENY_NAMES, DEFAULT_DENY_SUFFIXES, Config, workspace_path
from .errors import PolicyError

# 兼容旧引用；实际默认值集中在 config.py，便于配置化扩展。
SENSITIVE_NAMES = DEFAULT_DENY_NAMES
SENSITIVE_SUFFIXES = DEFAULT_DENY_SUFFIXES
READ_PURPOSES = frozenset({"read", "list"})
WRITE_PURPOSES = frozenset({"write", "mkdir", "move_source", "move_dest"})


@dataclass(frozen=True)
class Policy:
    config: Config

    @property
    def workspace(self) -> Path:
        return Path(os.path.realpath(workspace_path(self.config)))

    def roots(self, purpose: str) -> tuple[Path, ...]:
        """写操作只允许在主工作区；额外授权目录为只读。"""
        if purpose in WRITE_PURPOSES:
            return (self.workspace,)
        return (self.workspace,) + tuple(
            Path(os.path.realpath(Path(item).expanduser())) for item in self.config.extra_roots
        )

    def _rules(self, root: Path) -> tuple[set[str], set[str]]:
        """默认规则 + 全局追加 + 按工作区追加；任何配置都不能移除默认规则。"""
        names = {str(item).lower() for item in self.config.deny_names} | set(SENSITIVE_NAMES)
        suffixes = {str(item).lower() for item in self.config.deny_suffixes} | set(SENSITIVE_SUFFIXES)
        extra = self.config.workspace_rules.get(str(root), {})
        names |= {str(item).lower() for item in extra.get("deny_names", [])}
        suffixes |= {str(item).lower() for item in extra.get("deny_suffixes", [])}
        suffixes = {item if item.startswith(".") else "." + item for item in suffixes}
        return names, suffixes

    def _safe_parts(self, relative: Path, root: Path) -> None:
        names, suffixes = self._rules(root)
        for part in relative.parts:
            if part in ("", ".", ".."):
                continue
            if part.startswith(".") or part.lower() in names:
                raise PolicyError(f"拒绝访问隐藏或敏感路径组件：{part}")
            if PurePosixPath(part).suffix.lower() in suffixes:
                raise PolicyError(f"拒绝访问敏感文件类型：{part}")

    def path(self, raw: str, purpose: str) -> Path:
        """校验并解析工作区路径。

        - 相对路径以工作区为基准；绝对路径必须落在授权根目录内。
        - 拒绝 `..`、空字节、隐藏/敏感文件，以及逃逸到根目录外的符号链接。
        - 写入用途额外拒绝根目录内的任何符号链接跳转（含最终组件）。
        """
        if purpose not in READ_PURPOSES | WRITE_PURPOSES:
            raise PolicyError(f"未知路径用途：{purpose}")
        if not isinstance(raw, str) or not raw.strip():
            raise PolicyError("路径不能为空")
        if "\x00" in raw:
            raise PolicyError("路径包含非法空字节")
        if raw.lower().startswith(("file:", "http:", "https:")):
            raise PolicyError("路径参数不接受 URL")
        parts = PurePosixPath(raw.replace("\\", "/")).parts
        if ".." in parts:
            raise PolicyError("拒绝包含 '..' 的路径穿越")
        if raw.startswith("~") and not raw.startswith("~/"):
            raise PolicyError("不支持以其他用户家目录为目标的路径")

        raw_path = Path(raw).expanduser()
        base = workspace_path(self.config)
        candidate = raw_path if raw_path.is_absolute() else base / raw_path
        absolute = Path(os.path.abspath(candidate))
        real = Path(os.path.realpath(candidate))

        # 授权根本身可能是符号链接（例如某些发行版的 home 目录），分别按
        # 「用户输入的写法」和「真实路径」计算相对路径，再比较是否发生跳转。
        pairs: list[tuple[Path, Path]] = [(self.workspace, Path(os.path.abspath(workspace_path(self.config))))]
        if purpose not in WRITE_PURPOSES:
            for item in self.config.extra_roots:
                configured = Path(os.path.abspath(Path(item).expanduser()))
                pairs.append((Path(os.path.realpath(configured)), configured))
        rel_abs: Path | None = None
        rel_real: Path | None = None
        matched_root: Path | None = None
        for allowed_real, allowed_abs in pairs:
            try:
                rel_real = real.relative_to(allowed_real)
            except ValueError:
                continue
            try:
                rel_abs = absolute.relative_to(allowed_abs)
            except ValueError:
                # 路径通过根目录的符号链接别名给出，退回到真实相对路径。
                rel_abs = rel_real
            matched_root = allowed_real
            break
        if rel_abs is None or rel_real is None or matched_root is None:
            raise PolicyError(f"路径不在授权范围内：{raw}（允许的目录：{', '.join(str(r) for r in self.roots(purpose))}）")

        self._safe_parts(rel_abs, matched_root)
        if rel_real != rel_abs:
            # 符号链接指向授权树内的另一位置：读操作可放行，写操作一律拒绝。
            if purpose in WRITE_PURPOSES:
                raise PolicyError(f"拒绝通过符号链接写入：{raw}")
            self._safe_parts(rel_real, matched_root)
        if purpose == "write" and real.is_symlink():
            raise PolicyError(f"拒绝覆盖符号链接：{raw}")
        return real

    def url(self, raw: str) -> str:
        """仅允许 http(s) 且域名在白名单内的 URL；不执行任何自动点击/提交。"""
        if not isinstance(raw, str) or not raw.strip():
            raise PolicyError("URL 不能为空")
        try:
            parsed = urlsplit(raw.strip())
        except ValueError as exc:
            raise PolicyError(f"URL 格式无效：{exc}") from exc
        if parsed.scheme.lower() not in ("http", "https"):
            raise PolicyError(f"只允许 http/https URL，收到：{parsed.scheme!r}")
        if parsed.username or parsed.password:
            raise PolicyError("URL 不得包含用户名或密码")
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise PolicyError("URL 缺少域名")
        if any(ord(ch) < 32 for ch in raw):
            raise PolicyError("URL 包含控制字符")
        if parsed.port not in (None, 80, 443):
            raise PolicyError("只允许标准 HTTP/HTTPS 端口")
        allowed = {item.strip().lower().lstrip(".") for item in self.config.allowed_domains}
        if not any(host == domain or host.endswith("." + domain) for domain in allowed):
            raise PolicyError(f"域名不在白名单内：{host}。可在配置中添加 allowed_domains。")
        return parsed.geturl()

    def describe(self) -> str:
        roots = ", ".join(str(path) for path in self.roots("list"))
        read_roots = ", ".join(str(path) for path in self.roots("read"))
        domains = ", ".join(self.config.allowed_domains) or "（无）"
        return (
            f"可写工作区：{roots}\n"
            f"额外只读目录：{read_roots}\n"
            f"允许域名：{domains}"
        )
