from __future__ import annotations

import pytest

from freeagent.errors import PolicyError
from freeagent.policy import Policy

from .conftest import Env


def test_relative_path_inside_workspace(env: Env) -> None:
    policy = Policy(env.config)
    assert policy.path("docs/a.txt", "read") == env.workspace / "docs" / "a.txt"
    with pytest.raises(PolicyError):
        policy.path("/etc/passwd", "read")


def test_traversal_and_nul_rejected(env: Env) -> None:
    policy = Policy(env.config)
    for candidate in ("../secret.txt", "docs/../../secret", "a\x00b", "file:///etc/passwd"):
        with pytest.raises(PolicyError):
            policy.path(candidate, "read")


def test_hidden_and_sensitive_files_rejected(env: Env) -> None:
    policy = Policy(env.config)
    for name in (".env", ".ssh/id_rsa", "certs/server.pem", ".hidden/note.txt", "token.key"):
        with pytest.raises(PolicyError):
            policy.path(name, "read")


def test_symlink_escape_rejected_for_read_and_write(env: Env) -> None:
    (env.workspace / "link.txt").symlink_to("/etc/passwd")
    policy = Policy(env.config)
    with pytest.raises(PolicyError):
        policy.path("link.txt", "read")
    with pytest.raises(PolicyError):
        policy.path("link.txt", "write")

    (env.workspace / "inner").mkdir()
    (env.workspace / "inner" / "ok.txt").write_text("safe")
    (env.workspace / "inner-link").symlink_to(env.workspace / "inner", target_is_directory=True)
    assert policy.path("inner-link/ok.txt", "read") == env.workspace / "inner" / "ok.txt"
    with pytest.raises(PolicyError):
        policy.path("inner-link/new.txt", "write")


def test_extra_roots_are_read_only(env: Env, tmp_path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "public.txt").write_text("hello")
    config = env.config.model_copy(update={"extra_roots": [str(extra)]})
    policy = Policy(config)
    assert policy.path(str(extra / "public.txt"), "read") == extra / "public.txt"
    with pytest.raises(PolicyError):
        policy.path(str(extra / "public.txt"), "write")


def test_url_allowlist(env: Env) -> None:
    policy = Policy(env.config)
    assert policy.url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert policy.url("https://docs.example.com/x") == "https://docs.example.com/x"
    for bad in (
        "javascript:alert(1)", "http://evil.com", "https://user:pass@example.com",
        "https://example.com:8080", "file:///etc/passwd", "https://notexample.com",
    ):
        with pytest.raises(PolicyError):
            policy.url(bad)


def test_configurable_deny_rules_can_only_tighten(env: Env) -> None:
    cfg = env.config.model_copy(update={
        "deny_names": ["secret.txt"],
        "deny_suffixes": [".bak"],
    })
    policy = Policy(cfg)
    for name in ("secret.txt", "notes.bak", ".env", "id_rsa", "server.pem"):
        with pytest.raises(PolicyError):
            policy.path(name, "read")


def test_workspace_specific_deny_rules(env: Env) -> None:
    cfg = env.config.model_copy(update={
        "workspace_rules": {str(env.workspace): {"deny_names": ["local-only.txt"]}},
    })
    policy = Policy(cfg)
    with pytest.raises(PolicyError):
        policy.path("local-only.txt", "read")
    assert policy.path("normal.txt", "read") == env.workspace / "normal.txt"

    # 清空用户规则后，默认敏感名单仍然生效。
    cleared = env.config.model_copy(update={"deny_names": [], "deny_suffixes": []})
    with pytest.raises(PolicyError):
        Policy(cleared).path(".env", "read")
