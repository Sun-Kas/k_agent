"""Trusted, explicit pipeline definitions for built-in Agent kinds."""

from backend.agent.hooks.pipeline import AgentPipelineDefinition


def build_k_agent_pipeline_definition() -> AgentPipelineDefinition:
    """Compile K Agent extensions without runtime discovery or import strings."""

    # Permission, Skill allowlist, and schema validation are a sealed terminal
    # inside Tool Pipeline. Optional behavior middleware is listed here when added.
    return AgentPipelineDefinition.compile(middleware=())
