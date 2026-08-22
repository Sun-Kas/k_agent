"""Trusted, explicit pipeline definitions for built-in Agent kinds."""

from backend.agent.hooks.builtin_middleware import (
    strip_legacy_read_escalation_fields,
)
from backend.agent.hooks.pipeline import AgentPipelineDefinition


def build_k_agent_pipeline_definition() -> AgentPipelineDefinition:
    """Compile K Agent extensions without runtime discovery or import strings."""

    # 后续新增 Hook/Middleware 必须：
    # 1. 在 builtin_middleware.py 中实现并写清执行边界；
    # 2. 在下面列表中显式注册。Decorator 本身不会让 Hook 自动生效。
    # 列表顺序用于同 order 时的稳定排序；before 按 order 正序、after 逆序，
    # wrap 则由较小 order 包住较大 order。
    #
    # Permission、Skill allowlist 和 schema validation 始终留在 Tool Pipeline 的
    # sealed terminal 中，普通 Middleware 不得把这些安全门注册、移除或重新排序。
    return AgentPipelineDefinition.compile(
        middleware=(
            strip_legacy_read_escalation_fields,
        )
    )
