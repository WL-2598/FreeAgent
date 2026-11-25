"""项目异常类型：用于把「安全停止」和「普通错误」区分开来。"""


class FreeAgentError(Exception):
    """所有可预期的 FreeAgent 错误。"""


class ConfigError(FreeAgentError):
    """配置读写或初始化失败。"""


class PolicyError(FreeAgentError):
    """动作违反本地策略（路径、域名、权限范围等）。"""


class ActionError(FreeAgentError):
    """动作协议不合法或工具参数错误。"""


class ParseError(FreeAgentError):
    """模型响应无法解析为规定的 JSON 动作。"""


class ConnectorError(FreeAgentError):
    """连接器不可用、登录失效、限流、超时或界面异常。"""


class ToolError(FreeAgentError):
    """工具执行失败。"""


class Cancelled(FreeAgentError):
    """用户或外部指令中止了任务。"""


class NeedsApproval(FreeAgentError):
    """动作已校验完成，正等待用户明确批准。"""
