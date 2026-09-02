"""面向前端的 Access Layer 包：公开 API、会话/目录状态与协议适配。

与 Agent Backend 的边界：本包拥有会话、配置与并发；后端无状态，只接收
已装配的 Agent 运行请求（messages + MCP 配置 + Skill catalog 元数据等），
并在实际 Skill 工具调用后自行读取正文。
"""

from access_layer.gateway import AgentAccessLayer

__all__ = ["AgentAccessLayer"]
