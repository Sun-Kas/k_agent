"""面向前端的 Access Layer 包：公开 API、会话/目录状态与协议适配。

与 Agent Backend 的边界：本包拥有会话、配置与并发；后端无状态，只接收
已装配完整的 Agent 运行请求（messages + 自包含 MCP/Skill 等），执行模型与工具。
"""

from access_layer.gateway import AgentAccessLayer

__all__ = ["AgentAccessLayer"]
