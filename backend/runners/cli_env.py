"""Shared child-process environment for CLI agent runners."""

from __future__ import annotations

import os
from pathlib import Path

from backend.home import ensure_shared_runtime, link_shared_runtime, shared_runtime_tool_env
from backend.runners.base import RunnerContext


def build_cli_child_env(
    ctx: RunnerContext,
    *,
    workspace: Path | None = None,
) -> dict[str, str]:
    """Host env + project-wide Node/npm runtime for Codex / Claude Code shells.

    CLI agents own their Bash tools; we cannot scrub each shell spawn the way
    `cc_bash` does. Pinning PATH / npm prefix on the CLI process is the lever
    their child shells normally inherit.
    """

    runtime = ensure_shared_runtime()
    link_root = workspace if workspace is not None else ctx.workspace_dir
    if link_root is not None:
        link_shared_runtime(link_root, runtime)

    env = os.environ.copy()
    env.update(shared_runtime_tool_env(runtime))
    tool_env = ctx.options.get("toolEnv") if isinstance(ctx.options, dict) else None
    if isinstance(tool_env, dict):
        env.update(
            {str(key): str(value) for key, value in tool_env.items() if value is not None}
        )
    return env
