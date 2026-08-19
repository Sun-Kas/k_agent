"""Codex app-server Runner：双向审批挂起/恢复走共享 ApprovalBroker。

app-server 协议保持 stdin 打开做 JSON-RPC，因此命令/文件/MCP/elicitation
审批可经前端审批卡暂停再继续。会话 resume 仅在 agentOptions 显式开启时生效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uuid

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
)
from backend.runners.codex_app_server import CodexStreamState, run_codex_app_server
from backend.runners.codex_config import write_codex_mcp_config
from backend.runners.network_policy import network_access_enabled
from backend.runners.resolve_cli import resolve_cli


class CodexRunner:
    """进程内 Codex 单例；本轮状态只存在于 create_runtime 的函数作用域。"""

    __slots__ = ()
    kind = "codex"

    @asynccontextmanager
    async def create_runtime(
        self, context: RunnerContext
    ) -> AsyncIterator[dict[str, Any]]:
        """只组装本轮 Codex app-server 所需参数。"""
        ctx = context
        resolved = resolve_cli(self.kind)
        if resolved is None:
            raise RuntimeError(
                "Codex CLI not found. Install ChatGPT/Codex app or set K_AGENT_CODEX_PATH"
            )
        command = resolved.path

        if ctx.workspace_dir is None:
            raise RuntimeError("CodexRunner requires an Access Layer workspaceDir")
        workspace = ctx.workspace_dir
        workspace.mkdir(parents=True, exist_ok=True)
        skill_preamble = build_codex_skill_preamble(ctx.skills)
        prompt = build_cli_prompt(ctx)
        if skill_preamble:
            prompt = f"{skill_preamble}\n\n---\n\n{prompt}"
        mode = cli_session_mode(ctx)
        resume_id = resume_session_id(ctx) if mode == "resume" else None
        write_codex_mcp_config(workspace, ctx.mcp_servers)
        _write_codex_reasoning(workspace, ctx.reasoning_effort)

        if ctx.approval_broker is None:
            raise RuntimeError("CodexRunner requires an approval broker")
        yield {
            "command": command,
            "cwd": workspace,
            "prompt": prompt,
            "model": str(ctx.model_id) if ctx.model_id else None,
            "effort": ctx.reasoning_effort,
            "resume_thread_id": resume_id,
            "ephemeral": mode != "resume",
            "approval_broker": ctx.approval_broker,
            "public_thread_id": ctx.thread_id,
            "run_id": ctx.run_id,
            "network_access": network_access_enabled(ctx),
            "permission_mode": (
                "full_access"
                if ctx.options.get("permissionMode") == "full_access"
                else "default"
            ),
            "resume_authorization": (
                dict(ctx.resume_checkpoints[0])
                if len(ctx.resume_checkpoints) == 1
                else None
            ),
            "mapper": map_codex_event,
            # Codex shell inherits this process env; pin shared Node/npm here.
            "env": build_cli_child_env(ctx, workspace=workspace),
        }

    async def run_stream(
        self, context: RunnerContext
    ) -> AsyncIterator[dict[str, Any]]:
        """加载 Runtime 参数，并在这里执行 app-server loop。"""

        async with self.create_runtime(context) as runtime:
            async for event in run_codex_app_server(**runtime):
                yield event


_CODEX_EFFORT_VALUES = {"minimal", "low", "medium", "high", "xhigh"}


def build_codex_skill_preamble(skills: list[dict[str, Any]]) -> str:
    """Expose selected Skill packages using Codex-specific instructions."""

    parts: list[str] = []
    for skill in skills:
        name = str(skill.get("name") or skill.get("id") or "").strip()
        instructions = str(skill.get("instructions") or "").strip()
        if not instructions:
            continue
        file_path = _single_line(skill.get("filePath"))
        base_dir = _single_line(skill.get("baseDir"))
        path_lines: list[str] = []
        if file_path:
            path_lines.append(f"SKILL.md absolute path: {file_path}")
        if base_dir:
            path_lines.append(f"Skill package root: {base_dir}")
            # Codex is sandboxed with the session workspace as cwd. Preserve
            # the package root explicitly so Skill-relative reads are exact.
            path_lines.append(
                "Resolve relative paths in this Skill (including scripts/, "
                "references/, assets/, and templates/) against the Skill "
                "package root above."
            )
        header = f"[Codex Skill: {name}]" if name else "[Codex Skill]"
        parts.append("\n".join([header, *path_lines, instructions]))
    return "\n\n".join(parts)


def _single_line(value: Any) -> str:
    """Keep path metadata from altering the provider prompt structure."""

    return " ".join(str(value or "").split()).strip()


def _write_codex_reasoning(workspace: Path, effort: str | None) -> None:
    """Set model_reasoning_effort in .codex/config.toml via a managed marker block.

    Codex CLI reads reasoning effort from config.toml, not a CLI flag.
    Valid values: minimal, low, medium, high, xhigh.
    """

    normalized = (effort or "").strip().lower()
    if normalized in {"", "none"}:
        normalized = "medium"
    if normalized not in _CODEX_EFFORT_VALUES:
        normalized = "medium"
    config_dir = Path(workspace) / ".codex"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    marker_start = "# >>> k_agent managed reasoning >>>"
    marker_end = "# <<< k_agent managed reasoning <<<"
    managed_block = (
        f"{marker_start}\n"
        f'model_reasoning_effort = "{normalized}"\n'
        f'model_reasoning_summary = "detailed"\n'
        f'hide_agent_reasoning = false\n'
        f"{marker_end}\n"
    )
    existing = ""
    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8")
    if marker_start in existing:
        import re as _re
        existing = _re.sub(
            _re.escape(marker_start) + r".*?" + _re.escape(marker_end) + r"\n?",
            "",
            existing,
            flags=_re.DOTALL,
        )
    merged = existing.rstrip() + ("\n\n" if existing.strip() else "") + managed_block
    config_path.write_text(merged, encoding="utf-8")


_CHUNK_SIZE = 60


def _chunk_text(text: str) -> list[str]:
    """Split text into display-friendly chunks for simulated streaming.

    Prefers splitting at paragraph/sentence boundaries when possible.
    """
    if len(text) <= _CHUNK_SIZE:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _CHUNK_SIZE:
            chunks.append(remaining)
            break
        # Try paragraph break first, then sentence end, then word boundary
        best = -1
        for sep in ("\n\n", "\n", "。", ". ", "，", ", ", " "):
            pos = remaining.rfind(sep, 0, _CHUNK_SIZE)
            if pos > 0:
                best = pos + len(sep)
                break
        if best <= 0:
            best = _CHUNK_SIZE
        chunks.append(remaining[:best])
        remaining = remaining[best:]
    return chunks


def _codex_item_id(item: dict[str, Any]) -> str:
    """Extract a stable item identifier for deduplication."""
    return str(item.get("id") or item.get("item_id") or "")


def map_codex_event(payload: dict[str, Any], state: _CliStreamState) -> list[dict[str, Any]]:
    if not isinstance(state, CodexStreamState):
        raise TypeError("Codex event mapping requires CodexStreamState")
    event_type = str(payload.get("type") or "")
    events: list[dict[str, Any]] = []

    if event_type == "thread.started":
        thread_id = payload.get("thread_id") or payload.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            state.provider_session_id = thread_id
            events.append(
                {
                    "type": "status",
                    "payload": {"message": f"Codex thread {thread_id}"},
                }
            )
        return events

    if event_type in {"turn.started", "turn.completed"}:
        if event_type == "turn.completed":
            events.extend(close_thinking(state))
        events.append(
            {
                "type": "trace",
                "payload": {"entry": f"codex.{event_type}"},
            }
        )
        return events

    if event_type == "turn.failed":
        error = payload.get("error") or payload.get("message") or "Codex turn failed"
        return [emit_error(str(error))]

    if event_type == "error":
        message = payload.get("message") or payload.get("error") or "Codex error"
        return [emit_error(str(message))]

    if event_type in {"item.started", "item.updated", "item.completed"}:
        item = payload.get("item")
        if not isinstance(item, dict):
            return events
        item_type = str(item.get("type") or "")
        item_id = _codex_item_id(item)
        is_final = event_type == "item.completed"

        # ── Reasoning / thinking ──────────────────────────────────
        if item_type == "reasoning":
            summary_blocks = item.get("summary") or []
            text_parts: list[str] = []
            if isinstance(summary_blocks, list):
                for block in summary_blocks:
                    if isinstance(block, dict) and block.get("type") == "summary_text":
                        text_parts.append(str(block.get("text") or ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
            thinking_text = "\n".join(text_parts).strip()
            if thinking_text:
                events.extend(emit_thinking(state, thinking_text, finish=is_final))
            elif is_final:
                events.extend(close_thinking(state))
            return events

        # ── Text messages ─────────────────────────────────────────
        if item_type in {"agent_message", "message", "assistant_message"}:
            text = item.get("text") or item.get("content") or ""
            if isinstance(text, str) and text:
                events.extend(close_thinking(state))
                if is_final:
                    events.extend(close_text_message(state))
                # Codex emits text only on item.completed (no incremental
                # deltas). Chunk the text so the frontend renders progressively
                # instead of a single flash.
                for chunk in _chunk_text(text):
                    events.extend(append_text(state, chunk))
                if is_final:
                    events.extend(close_text_message(state))
            return events

        # ── Command execution ─────────────────────────────────────
        if item_type == "command_execution":
            events.extend(close_thinking(state))
            events.extend(close_text_message(state))
            raw_cmd = item.get("command") or ""
            name = str(raw_cmd)[:80] or "command"
            if event_type == "item.started":
                internal_id = str(uuid.uuid4())
                if item_id:
                    state.item_tool_map[item_id] = internal_id
                events.extend(emit_tool_call(name=name, arguments=raw_cmd, tool_call_id=internal_id))
            elif is_final:
                result = str(item.get("aggregated_output") or item.get("output") or "")
                exit_code = item.get("exit_code")
                if exit_code is not None:
                    result = f"[exit {exit_code}]\n{result}"
                paired_id = state.item_tool_map.pop(item_id, None) if item_id else None
                if paired_id:
                    events.extend(emit_tool_result(tool_call_id=paired_id, content=result))
                else:
                    events.extend(emit_tool_call(name=name, arguments=raw_cmd, result=result))
            return events

        # ── MCP tool calls ────────────────────────────────────────
        # Codex struct: McpToolCallItem { server, tool, arguments, result, error, status }
        if item_type == "mcp_tool_call":
            events.extend(close_thinking(state))
            events.extend(close_text_message(state))
            server_label = str(item.get("server") or "")
            raw_tool = str(item.get("tool") or item.get("name") or item.get("tool_name") or "mcp_tool")
            tool_name = f"{server_label}/{raw_tool}" if server_label else raw_tool
            tool_name = tool_name[:80]
            arguments = item.get("arguments") or {}
            if event_type == "item.started":
                internal_id = str(uuid.uuid4())
                if item_id:
                    state.item_tool_map[item_id] = internal_id
                events.extend(emit_tool_call(name=tool_name, arguments=arguments, tool_call_id=internal_id))
            elif is_final:
                error = item.get("error")
                result_data = item.get("result") or {}
                if isinstance(result_data, dict):
                    content_blocks = result_data.get("content") or []
                    result_text = "\n".join(
                        str(b.get("text") or b) for b in content_blocks
                    ) if isinstance(content_blocks, list) else str(result_data)
                else:
                    result_text = str(result_data)
                if error and isinstance(error, dict):
                    err_msg = str(error.get("message") or error.get("error") or error)
                    result_text = f"[ERROR] {err_msg}" + (f"\n{result_text}" if result_text else "")
                elif not result_text or result_text in ("{}", "None", ""):
                    result_text = "(empty result)"
                paired_id = state.item_tool_map.pop(item_id, None) if item_id else None
                if paired_id:
                    events.extend(emit_tool_result(tool_call_id=paired_id, content=result_text))
                else:
                    events.extend(emit_tool_call(name=tool_name, arguments=arguments, result=result_text))
            return events

        # ── File changes ──────────────────────────────────────────
        if item_type == "file_change":
            events.extend(close_thinking(state))
            events.extend(close_text_message(state))
            changes = item.get("changes") or []
            paths = ", ".join(
                str(c.get("path") or "") for c in changes if isinstance(c, dict)
            ) if isinstance(changes, list) else str(item.get("path") or "")
            if event_type == "item.started":
                internal_id = str(uuid.uuid4())
                if item_id:
                    state.item_tool_map[item_id] = internal_id
                events.extend(emit_tool_call(name="file_change", arguments=item, tool_call_id=internal_id))
            elif is_final:
                paired_id = state.item_tool_map.pop(item_id, None) if item_id else None
                if paired_id:
                    events.extend(emit_tool_result(tool_call_id=paired_id, content=paths))
                else:
                    events.extend(emit_tool_call(name="file_change", arguments=item, result=paths))
            return events

        # ── Web search ─────────────────────────────────────────────
        if item_type == "web_search":
            events.extend(close_thinking(state))
            events.extend(close_text_message(state))
            query = str(item.get("query") or item.get("keyword") or "web_search")[:80]
            if event_type == "item.started":
                internal_id = str(uuid.uuid4())
                if item_id:
                    state.item_tool_map[item_id] = internal_id
                events.extend(emit_tool_call(name="web_search", arguments=query, tool_call_id=internal_id))
            elif is_final:
                results = item.get("results") or item.get("output") or ""
                result_text = str(results)[:2000] if results else "(no results)"
                paired_id = state.item_tool_map.pop(item_id, None) if item_id else None
                if paired_id:
                    events.extend(emit_tool_result(tool_call_id=paired_id, content=result_text))
                else:
                    events.extend(emit_tool_call(name="web_search", arguments=query, result=result_text))
            return events

        # ── Collab tool calls ─────────────────────────────────────
        if item_type == "collab_tool_call":
            events.extend(close_thinking(state))
            events.extend(close_text_message(state))
            raw_tool = str(item.get("tool") or item.get("name") or "collab_tool")[:80]
            arguments = item.get("arguments") or {}
            if event_type == "item.started":
                internal_id = str(uuid.uuid4())
                if item_id:
                    state.item_tool_map[item_id] = internal_id
                events.extend(emit_tool_call(name=raw_tool, arguments=arguments, tool_call_id=internal_id))
            elif is_final:
                result_data = item.get("result") or item.get("output") or ""
                result_text = str(result_data)[:2000] if result_data else "(no result)"
                paired_id = state.item_tool_map.pop(item_id, None) if item_id else None
                if paired_id:
                    events.extend(emit_tool_result(tool_call_id=paired_id, content=result_text))
                else:
                    events.extend(emit_tool_call(name=raw_tool, arguments=arguments, result=result_text))
            return events

        # ── Other item types (todo_list, etc.) ────────────────────
        events.append(
            {
                "type": "trace",
                "payload": {"entry": f"codex.{event_type}", "output": item_type},
            }
        )
        return events

    events.append(
        {
            "type": "trace",
            "payload": {"entry": f"codex.{event_type or 'event'}"},
        }
    )
    return events
