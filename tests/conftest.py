"""测试夹具：每个用例使用隔离的临时 home/工作区，绝不触碰真实用户目录。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from freeagent.audit import AuditLog
from freeagent.config import Config, Layout, setup
from freeagent.connectors import MockConnector
from freeagent.orchestrator import RecordingUI, TaskRunner


@dataclass
class Env:
    home: Path
    workspace: Path
    config: Config
    layout: Layout

    def runner(self, responses: list[str], *, approve: bool = True, config: Config | None = None) -> TaskRunner:
        cfg = config or self.config
        return TaskRunner(
            cfg,
            layout=self.layout,
            audit=AuditLog(self.layout),
            ui=RecordingUI(approve=approve),
        )

    def connector(self, responses: list[str]) -> MockConnector:
        return MockConnector(responses)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FREEAGENT_HOME", str(home))
    monkeypatch.setenv("FREEAGENT_WORKSPACE", str(workspace))
    layout = Layout(home)
    cfg, _ = setup(layout)
    return Env(home, workspace, cfg, layout)


def action(name: str, **arguments: object) -> str:
    import json

    return json.dumps({"action": name, "arguments": arguments, "reason": "test"}, ensure_ascii=False)
