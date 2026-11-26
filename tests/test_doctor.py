"""自检入口测试：Claude 拉取后可以先用它确认环境。"""

from freeagent.doctor import run_doctor

from .conftest import Env


def test_doctor_reports_ready_environment(env: Env) -> None:
    result = run_doctor(env.config, env.layout)
    assert result["ok"] is True
    names = {item["name"] for item in result["checks"]}
    assert {"Python 版本", "配置文件", "工作区存在", "工作区可写", "审计数据库", "MCP 依赖", "手动连接器", "网页自动化默认禁用"} <= names
    assert result["tool_count"] == 17
    assert result["write_enabled"] is False
    assert "openclaw mcp doctor freeagent --probe" in result["next_steps"]


def test_doctor_reports_missing_workspace(env: Env) -> None:
    bad = env.config.model_copy(update={"workspace": str(env.home / "does-not-exist")})
    result = run_doctor(bad, env.layout)
    assert result["ok"] is False
    workspace_check = next(item for item in result["checks"] if item["name"] == "工作区存在")
    assert workspace_check["ok"] is False
