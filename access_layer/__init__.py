"""面向前端的 Access Layer 包：公开 API、会话/目录状态与协议适配。

与 Agent Backend 的边界：本包拥有会话、配置与并发；后端无状态，只接收
已装配的 Agent 运行请求（messages + MCP 配置 + Skill catalog 元数据等），
并在实际 Skill 工具调用后自行读取正文。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from access_layer.gateway import AgentAccessLayer


def __getattr__(name: str) -> Any:
    """延迟导入网关，避免 `python -m access_layer.sessions.migrate_history`
    在包初始化时又反向加载迁移模块。
    """

    if name == "AgentAccessLayer":
        from access_layer.gateway import AgentAccessLayer

        return AgentAccessLayer
    raise AttributeError(name)

__all__ = ["AgentAccessLayer"]
