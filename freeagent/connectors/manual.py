"""手动粘贴连接器：MVP 默认且始终可用。

程序展示提示词，用户在自己已登录的浏览器里复制模型回复并粘贴回来；
本地继续做结构校验、策略校验和受限执行。不存储账号、Cookie 或网站密码。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from ..errors import ConnectorError
from .base import AskContext, Connector, Health

END_MARKER = "END"


class ManualConnector:
    id = "manual"
    label = "手动粘贴（默认，无需自动化）"

    def __init__(
        self,
        responses: str | Path | None = None,
        *,
        input_fn: Callable[[str], str] | None = None,
        output_fn: Callable[[str], None] | None = None,
    ) -> None:
        # responses 可以是单个回复文件，也可以是按轮次命名的目录（01.txt, 02.txt ...）。
        self.responses = Path(responses).expanduser() if responses else None
        self._input = input_fn or (lambda prompt: input(prompt))
        self._output = output_fn or (lambda text: print(text, flush=True))

    def health(self) -> Health:
        return Health(True, "可用", "提示词由程序展示，模型回复由用户手动粘贴")

    def login(self) -> str:
        return "手动模式无需登录：请在你自己已登录的浏览器中访问所选模型网站。"

    def _response_from_file(self, step: int, round_index: int) -> str:
        assert self.responses is not None
        if self.responses.is_dir():
            candidates = [
                self.responses / f"{round_index:02d}.txt",
                self.responses / f"{step:02d}.txt",
                self.responses / f"response-{round_index}.txt",
            ]
            path = next((item for item in candidates if item.exists()), None)
        else:
            path = self.responses
        if path is None or not path.exists():
            raise ConnectorError(f"找不到第 {round_index} 轮的回复文件（目录：{self.responses}）")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConnectorError(f"读取回复文件失败：{exc}") from exc

    def ask(self, prompt: str, context: AskContext) -> str:
        if self.responses is not None:
            response = self._response_from_file(context.step, context.round_index)
            self._output(f"[manual] 第 {context.round_index} 轮回复已从文件读取（{len(response)} 字符）。")
            return response
        self._output("=" * 68)
        self._output(f"[手动模式] 任务 {context.task_id} · 第 {context.round_index} 轮 · 请把下面的提示词复制到你已登录的网页模型：")
        self._output("=" * 68)
        self._output(prompt)
        self._output("-" * 68)
        self._output(f"[手动模式] 粘贴模型回复，以单独一行 {END_MARKER} 结束；直接 EOF 表示中止。")
        lines: list[str] = []
        while True:
            try:
                line = self._input("> ")
            except EOFError:
                raise ConnectorError("输入结束，任务已中止") from None
            if line.strip() == END_MARKER:
                break
            lines.append(line)
        response = "\n".join(lines)
        if not response.strip():
            raise ConnectorError("模型回复为空，任务已中止")
        return response


class MockConnector:
    """脚本化连接器，仅用于测试/演示；永不访问网络。"""

    id = "mock"
    label = "模拟模型（仅测试/演示）"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._used = 0

    def health(self) -> Health:
        return Health(True, "可用（仅测试）", "本地脚本响应，不访问网络；CLI 默认无脚本，演示请用 freeagent demo")

    def login(self) -> str:
        return "模拟连接器无需登录。"

    def ask(self, prompt: str, context: AskContext) -> str:
        if self._used >= len(self._responses):
            raise ConnectorError("模拟回复已用尽；请增加脚本响应或检查任务步数")
        response = self._responses[self._used]
        self._used += 1
        return response


class WebConnectorStub:
    """网页自动化占位连接器：未验证网站规则前一律不可用，不做任何规避。"""

    id = "web"
    label = "网页自动化（未启用）"

    def health(self) -> Health:
        return Health(
            False,
            "未启用 / 未验证",
            "必须先确认目标网站当前服务条款允许自动化，并实现独立适配器后才可启用；"
            "在此之前请使用手动粘贴模式。",
        )

    def login(self) -> str:
        raise ConnectorError(
            "网页自动化未启用：需先按目标网站当前条款验证是否允许自动化。"
            "本产品不绕过验证码、速率限制或付费墙；请使用手动粘贴模式。"
        )

    def ask(self, prompt: str, context: AskContext) -> str:
        raise ConnectorError(
            "网页自动化连接器未启用。请使用 --connector manual；"
            "未验证条款前不会启动浏览器自动化，也不提供规避方案。"
        )


def resolve_connector(name: str, *, responses: str | Path | None = None) -> Connector:
    name = (name or "manual").lower()
    if name == "manual":
        return ManualConnector(responses)
    if name == "mock":
        return MockConnector([])
    if name in {"web", "deepseek_web", "chatgpt_web", "gemini_web"}:
        return WebConnectorStub()
    raise ConnectorError(
        f"未知连接器：{name}。当前可用：manual（手动粘贴）、mock（仅测试）；"
        "网页自动化连接器在条款验证前不可用。"
    )


__all__ = ["AskContext", "Health", "ManualConnector", "MockConnector", "WebConnectorStub", "resolve_connector"]
