"""Claude Code CLI Runner（`claude -p --output-format stream-json`）。

默认 permission mode 为 ``bypassPermissions``，以便无头执行下 MCP/工具可用
（stdin 为 /dev/null）。调用方可经 agentOptions ``claudePermissionMode`` 收紧；
需要人类审批时走 approval bridge MCP。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from backend.runners.claude_approval_bridge import (
    APPROVAL_TOOL_NAME,
    ClaudeApprovalBridge,
)
from backend.runners.base import RunnerContext
from backend.runners.cli_env import build_cli_child_env
from backend.runners.cli_process import (
    _CliStreamState,
    append_text,
    build_cli_prompt,
    cli_session_mode,
    close_text_message,
    close_thinking,
    emit_error,
    emit_thinking,
    emit_tool_call,
    emit_tool_result,
    resume_session_id,
    run_cli_jsonl,
)
from backend.runners.claude_mcp import (
    inject_claude_mcp_secrets,
    write_claude_mcp_config,
)
from backend.runners.resolve_cli import resolve_cli
from backend.runners.network_policy import network_access_enabled


class ClaudeCodeRunner:
    """进程内 Claude 单例；本轮状态只存在于 create_runtime 的函数作用域。"""

    __slots__ = ()
    kind = "claude_code"

    @asynccontextmanager
    async def create_runtime(
        self, context: RunnerContext
    ) -> AsyncIterator[dict[str, Any]]:
        """只组装本轮 Claude CLI 参数，并维持审批 bridge 生命周期。"""
        ctx = context
        resolved = resolve_cli(self.kind)
        if resolved is None:
            raise RuntimeError(
                "Claude Code CLI not found. Install `claude` / `claude-internal` "
                "or set K_AGENT_CLAUDE_PATH"
            )
        command = resolved.path

        if ctx.workspace_dir is None:
            raise RuntimeError("ClaudeCodeRunner requires an Access Layer workspaceDir")
        workspace = ctx.workspace_dir
        workspace.mkdir(parents=True, exist_ok=True)
        skill_preamble = build_claude_skill_preamble(ctx.skills)
        prompt = build_cli_prompt(ctx)
        if skill_preamble:
            prompt = f"{skill_preamble}\n\n---\n\n{prompt}"
        if ctx.mcp_servers:
            # CLI providers expose different MCP tool names and authentication
            # states. Explicitly forbid carrying a Codex/K Agent tool spelling
            # into Claude Code, which must use only its init-time tool catalog.
            prompt = (
                "[Claude Code MCP rules]\n"
                "Use only exact MCP tool names present in this Claude Code run. "
                "Never infer or translate tool names from earlier providers. "
                "If a selected server exposes only authentication tools, "
                "authenticate first and report the authentication error instead "
                "of guessing a business tool name.\n\n"
                f"{prompt}"
            )
        mode = cli_session_mode(ctx)
        resume_id = resume_session_id(ctx) if mode == "resume" else None
        async with _approval_bridge(ctx) as approval_bridge:
            full_access = ctx.options.get("permissionMode") == "full_access"
            permission_mode = _claude_permission_mode(
                ctx, approval_available=approval_bridge is not None
            )
            if full_access:
                permission_mode = "bypassPermissions"
            claude_mcp_servers = list(ctx.mcp_servers)
            if approval_bridge is not None:
                # Even bypassPermissions cannot answer requiresUserInteraction
                # tools such as AskUserQuestion. Keep the private prompt bridge
                # installed in full-access runs while ordinary tools remain
                # bypassed by Claude's own permission mode.
                claude_mcp_servers.append(approval_bridge.mcp_server())
            mcp_config = write_claude_mcp_config(workspace, claude_mcp_servers)

            argv = [
                command,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--permission-mode",
                permission_mode,
            ]
            # Flag settings have run scope and do not mutate the user's Claude
            # configuration. `*` permits outbound hosts while leaving Claude's
            # filesystem sandbox enabled; an empty list is a fail-closed deny.
            argv.extend([
                "--settings",
                json.dumps(
                    claude_sandbox_settings(
                        network_access_enabled(ctx), full_access=full_access
                    ),
                    separators=(",", ":"),
                ),
            ])
            if approval_bridge is not None:
                # Claude print mode delegates permission prompts to this private
                # MCP tool. Under bypassPermissions it is still needed for
                # requiresUserInteraction tools; normal permissions stay bypassed.
                argv.extend([
                    "--allowedTools", *_claude_allowed_tools(ctx),
                    "--permission-prompt-tool", APPROVAL_TOOL_NAME,
                ])
            if mcp_config is not None:
                # A run must see exactly the MCP servers selected in the K Agent
                # composer, not unrelated user/project Claude configurations.
                argv.extend(["--strict-mcp-config", "--mcp-config", str(mcp_config)])
            if ctx.model_id:
                argv.extend(["--model", str(ctx.model_id)])
            effort = _claude_effort(ctx.reasoning_effort)
            if effort is not None:
                argv.extend(["--effort", effort])
            if resume_id:
                argv.extend(["--resume", resume_id])
            elif mode == "resume":
                argv.append("--continue")

            env = build_cli_child_env(ctx, workspace=workspace)
            inject_claude_mcp_secrets(env, ctx.mcp_servers)
            if approval_bridge is not None:
                env.update(approval_bridge.child_env())
            yield {
                "argv": argv,
                "cwd": workspace,
                "env": env,
                "mapper": map_claude_event,
                "kind": self.kind,
                "state_factory": ClaudeCodeStreamState,
            }

    async def run_stream(
        self, context: RunnerContext
    ) -> AsyncIterator[dict[str, Any]]:
        """加载 Runtime 参数，并在这里执行 Claude JSONL loop。"""

        async with self.create_runtime(context) as runtime:
            async for event in run_cli_jsonl(**runtime):
                yield event


class ClaudeCodeStreamState(_CliStreamState):
    """Claude-only reconciliation state for partial and snapshot messages."""

    def __init__(self) -> None:
        super().__init__()
        self.text_delta_buffer = ""
        self.thinking_delta_buffer = ""
        # Claude provider tool_use id → AG-UI tool call id.
        self.tool_id_map: dict[str, str] = {}


def claude_sandbox_settings(
    network_access: bool, *, full_access: bool = False
) -> dict[str, Any]:
    """Build run-scoped Claude settings; full access explicitly disables sandboxing."""

    return {
        "sandbox": {
            "enabled": not full_access,
            "network": {"allowedDomains": ["*"] if network_access else []},
        }
    }


def build_claude_skill_preamble(skills: list[dict[str, Any]]) -> str:
    """List selected skills from request catalog metadata. Do not load SKILL.md."""

    parts: list[str] = []
    for skill in skills:
        block = _catalog_skill_block(skill, "Claude Code Skill")
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def _catalog_skill_block(skill: dict[str, Any], label: str) -> str:
    name = str(skill.get("name") or skill.get("id") or "").strip()
    if not name:
        return ""
    lines = [f"[{label}: {name}]"]
    description = str(skill.get("description") or "").strip()
    if description:
        lines.append(description)
    when_to_use = str(skill.get("whenToUse") or skill.get("when_to_use") or "").strip()
    if when_to_use and when_to_use not in description:
        lines.append(f"when_to_use: {when_to_use}")
    hint = str(skill.get("argumentHint") or skill.get("argument_hint") or "").strip()
    if hint:
        lines.append(f"args: {hint}")
    file_path = _single_line(skill.get("filePath"))
    base_dir = _single_line(skill.get("baseDir"))
    if file_path:
        lines.append(f"SKILL.md absolute path: {file_path}")
    if base_dir:
        lines.append(f"Skill package root: {base_dir}")
        lines.append(
            "Resolve relative paths in this Skill (including scripts/, "
            "references/, assets/, and templates/) against the Skill "
            "package root above."
        )
    return "\n".join(lines)


def _single_line(value: Any) -> str:
    """Keep path metadata from altering the provider prompt structure."""

    return " ".join(str(value or "").split()).strip()


def map_claude_event(payload: dict[str, Any], state: _CliStreamState) -> list[dict[str, Any]]:
    if not isinstance(state, ClaudeCodeStreamState):
        raise TypeError("Claude event mapping requires ClaudeCodeStreamState")
    event_type = str(payload.get("type") or "")
    events: list[dict[str, Any]] = []

    if event_type == "system":
        subtype = str(payload.get("subtype") or "")
        session_id = payload.get("session_id") or payload.get("sessionId")
        if isinstance(session_id, str) and session_id:
            state.provider_session_id = session_id
        if subtype == "init":
            mcp_servers = payload.get("mcp_servers")
            needs_auth = [
                str(server.get("name") or "")
                for server in mcp_servers or []
                if isinstance(server, dict)
                and str(server.get("status") or "") == "needs-auth"
                and server.get("name")
            ]
            message = f"Claude Code ready" + (
                f" ({session_id})" if session_id else ""
            )
            if needs_auth:
                message += " · MCP 需要认证: " + ", ".join(needs_auth)
            events.append(
                {
                    "type": "status",
                    "payload": {
                        "message": message,
                        "mcpServers": mcp_servers if isinstance(mcp_servers, list) else [],
                        "mcpNeedsAuth": needs_auth,
                    },
                }
            )
        return events

    # ── Streaming deltas (real-time incremental content) ──────────────
    if event_type == "stream_event":
        event = payload.get("event")
        if not isinstance(event, dict):
            return events
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return events
        delta_type = str(delta.get("type") or "")
        if delta_type == "thinking_delta":
            thinking = delta.get("thinking") or ""
            if isinstance(thinking, str) and thinking:
                state.thinking_delta_buffer += thinking
                events.extend(emit_thinking(state, thinking))
        elif delta_type == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                state.text_delta_buffer += text
                events.extend(close_thinking(state))
                events.extend(append_text(state, text))
        elif delta_type == "input_json_delta":
            # Tool argument streaming; captured by the completed assistant block.
            pass
        return events

    # ── Complete assistant message (includes thinking, text, tool_use blocks) ─
    if event_type == "assistant":
        message_payload = payload.get("message")
        if not isinstance(message_payload, dict):
            return events
        content = message_payload.get("content")
        if isinstance(content, str) and content:
            events.extend(close_thinking(state))
            events.extend(_complete_claude_text(state, content))
            return events
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "")
                if block_type == "thinking":
                    thinking = block.get("thinking") or ""
                    if isinstance(thinking, str) and thinking:
                        events.extend(_complete_claude_thinking(state, thinking))
                elif block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        events.extend(close_thinking(state))
                        events.extend(_complete_claude_text(state, text))
                elif block_type == "tool_use":
                    events.extend(close_thinking(state))
                    events.extend(close_text_message(state))
                    tool_use_id = str(block.get("id") or "")
                    internal_id = str(uuid.uuid4())
                    if tool_use_id:
                        state.tool_id_map[tool_use_id] = internal_id
                    events.extend(
                        emit_tool_call(
                            name=str(block.get("name") or "tool"),
                            arguments=block.get("input") or {},
                            tool_call_id=internal_id,
                        )
                    )
        return events

    # ── Tool results (user-role messages carrying tool_result blocks) ─────
    if event_type == "user":
        message_payload = payload.get("message")
        if isinstance(message_payload, dict):
            content = message_payload.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("type") or "") == "tool_result":
                        tool_use_id = str(
                            block.get("tool_use_id")
                            or block.get("toolUseId")
                            or ""
                        )
                        result = block.get("content")
                        if isinstance(result, (dict, list)):
                            result_text = json.dumps(result, ensure_ascii=False)
                        else:
                            result_text = str(result or "")
                        paired_id = state.tool_id_map.pop(tool_use_id, None)
                        if paired_id:
                            events.extend(
                                emit_tool_result(
                                    tool_call_id=paired_id,
                                    content=result_text,
                                )
                            )
                        else:
                            events.extend(
                                emit_tool_call(
                                    name="tool_result",
                                    arguments={"toolUseId": tool_use_id},
                                    result=result_text,
                                )
                            )
        return events

    # ── Final result ─────────────────────────────────────────────────
    if event_type == "result":
        session_id = payload.get("session_id") or payload.get("sessionId")
        if isinstance(session_id, str) and session_id:
            state.provider_session_id = session_id
        events.extend(close_thinking(state))
        final_result = payload.get("result")
        if isinstance(final_result, str) and final_result and not state.saw_final_text:
            events.extend(close_text_message(state))
            events.extend(append_text(state, final_result))
            events.extend(close_text_message(state))
        else:
            events.extend(close_text_message(state))
        if payload.get("is_error"):
            events.append(emit_error(str(payload.get("result") or "Claude Code reported an error")))
        return events

    events.append(
        {
            "type": "trace",
            "payload": {"entry": f"claude_code.{event_type or 'event'}"},
        }
    )
    return events


def _complete_claude_text(
    state: _CliStreamState, snapshot: str
) -> list[dict[str, Any]]:
    """Reconcile a completed Claude text block with its streamed prefix."""

    if not isinstance(state, ClaudeCodeStreamState):
        raise TypeError("Claude text reconciliation requires ClaudeCodeStreamState")
    streamed = state.text_delta_buffer
    state.text_delta_buffer = ""
    if not streamed:
        return append_text(state, snapshot)
    if snapshot.startswith(streamed):
        return append_text(state, snapshot[len(streamed):])
    # A non-prefix snapshot represents a distinct provider block; retain it
    # instead of silently dropping content on an unexpected event sequence.
    return append_text(state, snapshot)


def _complete_claude_thinking(
    state: _CliStreamState, snapshot: str
) -> list[dict[str, Any]]:
    """Finish Claude reasoning without replaying streamed thinking deltas."""

    if not isinstance(state, ClaudeCodeStreamState):
        raise TypeError("Claude thinking reconciliation requires ClaudeCodeStreamState")
    streamed = state.thinking_delta_buffer
    state.thinking_delta_buffer = ""
    if not streamed:
        return emit_thinking(state, snapshot, finish=True)
    if snapshot.startswith(streamed):
        return emit_thinking(state, snapshot[len(streamed):], finish=True)
    events = close_thinking(state)
    events.extend(emit_thinking(state, snapshot, finish=True))
    return events


_CLAUDE_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions"}


def _claude_permission_mode(
    ctx: RunnerContext, *, approval_available: bool = False
) -> str:
    """Resolve Claude Code --permission-mode from agentOptions.

    Headless execution can ask only when the private permission-prompt MCP is
    active. Otherwise preserve the historical bypass fallback so tools are not
    silently rejected with "user cancelled".
    """
    configured = ctx.options.get("claudePermissionMode")
    raw = str(configured or "").strip()
    if raw in _CLAUDE_PERMISSION_MODES:
        return raw
    return "default" if approval_available else "bypassPermissions"


def _claude_allowed_tools(ctx: RunnerContext) -> list[str]:
    """Pre-approve routine built-ins while keeping the approval MCP callable."""

    configured = ctx.options.get("claudeAutoApproveTools")
    if isinstance(configured, list):
        routine_tools = [
            str(tool).strip()
            for tool in configured
            if isinstance(tool, str) and str(tool).strip()
        ]
    elif ctx.settings is not None:
        routine_tools = list(ctx.settings.claude_auto_approve_tools)
    else:
        routine_tools = [
            "Bash", "Read", "Edit", "Write", "Glob", "Grep",
            "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite",
            "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Skill",
        ]
    # Keep ordering stable for readable process diagnostics and avoid passing
    # the private permission tool twice when a caller supplies an override.
    return list(dict.fromkeys([*routine_tools, APPROVAL_TOOL_NAME]))


@asynccontextmanager
async def _approval_bridge(ctx: RunnerContext):
    """Keep the per-run callback alive for exactly the Claude subprocess run."""

    if ctx.approval_broker is None:
        yield None
        return
    async with ClaudeApprovalBridge(
        broker=ctx.approval_broker,
        thread_id=ctx.thread_id,
        run_id=ctx.run_id,
        resume_authorization=(
            dict(ctx.resume_checkpoints[0])
            if len(ctx.resume_checkpoints) == 1
            else None
        ),
    ) as bridge:
        yield bridge


_CLAUDE_EFFORT_VALUES = {"min", "low", "medium", "high", "max"}


def _claude_effort(effort: str | None) -> str | None:
    """Map a reasoning effort level to a Claude Code --effort value."""

    if not effort:
        return None
    normalized = effort.strip().lower()
    if normalized in {"", "none"}:
        return None
    return normalized if normalized in _CLAUDE_EFFORT_VALUES else None
