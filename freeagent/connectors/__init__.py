"""网页模型连接器：默认手动粘贴；网页自动化需逐站验证条款后另行实现。"""

from .base import AskContext, Connector, Health
from .manual import ManualConnector, MockConnector, WebConnectorStub, resolve_connector

__all__ = [
    "AskContext", "Connector", "Health", "ManualConnector", "MockConnector",
    "WebConnectorStub", "resolve_connector",
]
