"""browser.open：只打开用户授权域名，不读取页面、不自动点击或提交。"""

from __future__ import annotations

import os
import webbrowser

from ..config import Config
from ..errors import ToolError
from ..policy import Policy
from ..protocol import Action, BrowserOpen
from .base import Execution, Preview


class BrowserOpenTool:
    name = "browser.open"

    def preview(self, action: Action, policy: Policy) -> Preview:
        assert isinstance(action, BrowserOpen)
        url = policy.url(action.arguments.url)
        return Preview(
            [f"在可见浏览器中打开：{url}", "不会自动点击、填写表单或读取页面内容"],
            [url],
            "中（打开外部页面）",
        )

    def execute(self, action: Action, policy: Policy, config: Config) -> Execution:
        assert isinstance(action, BrowserOpen)
        url = policy.url(action.arguments.url)  # 执行前再次校验
        try:
            opened = webbrowser.open(url, new=2)
        except Exception as exc:  # webbrowser 可能抛出浏览器实现相关异常
            raise ToolError(f"打开浏览器失败：{type(exc).__name__}") from exc
        if not opened:
            message = (
                f"当前环境未能打开浏览器（可能没有图形界面）。请手动打开：{url}"
                if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                else f"浏览器未能打开：{url}"
            )
            return Execution(message, message)
        return Execution(f"已在浏览器中打开：{url}；页面内容未自动读取。", f"已打开：{url}")
