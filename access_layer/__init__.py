"""Frontend-facing access layer.

This package owns protocol adaptation, validation, session state and prompt
assembly. The agent backend receives only a complete AgentRunRequest.
"""

from access_layer.gateway import AgentAccessLayer

__all__ = ["AgentAccessLayer"]
