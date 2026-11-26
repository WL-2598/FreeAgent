"""提示词测试：历史摘要要能抑制网页模型重复同一个动作。"""

from freeagent.planner import build_prompt
from freeagent.policy import Policy

from .conftest import Env


def test_prompt_includes_action_history_and_no_repeat_rule(env: Env) -> None:
    prompt = build_prompt(
        "列出工作区文件",
        "（空目录，共 0 项：/tmp/ws）",
        Policy(env.config),
        step=2,
        max_steps=5,
        history=[{"seq": 1, "decision": "auto", "action": "fs.list {\"path\":\".\"}", "result": "列出 0 项"}],
    )
    assert "已执行动作摘要" in prompt
    assert "#1 [auto] fs.list" in prompt
    assert "不要重复已成功且结果未变化的动作" in prompt
    assert "task.done" in prompt
