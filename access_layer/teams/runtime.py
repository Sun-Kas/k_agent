"""Background scheduler that turns durable Team tasks into isolated Agent runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from access_layer.agent_backend_client import AgentBackendClient
from access_layer.catalog import RuntimeCatalog
from access_layer.teams.store import TeamStore


logger = logging.getLogger("k_agent.access.team_runtime")


class TeamRuntime:
    """Dispatch dependency-ready tasks while preserving Team state in SQLite."""

    def __init__(
        self,
        *,
        store: TeamStore,
        backend_client: AgentBackendClient,
        runtime_catalog: RuntimeCatalog,
        project_root: Path,
        max_active_runs: int = 8,
        task_lease_seconds: int = 120,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self._backend_client = backend_client
        self._runtime_catalog = runtime_catalog
        self._project_root = project_root.resolve()
        self._semaphore = asyncio.Semaphore(max_active_runs)
        self._task_lease_seconds = task_lease_seconds
        self.enabled = enabled
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    async def start(self) -> None:
        """Start one process-local dispatcher after the durable store is ready."""

        await self.store.initialize()
        await self.store.recover_expired_leases()
        self._stopping = False
        if not self.enabled:
            return
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="k-agent-team-scheduler"
        )

    async def stop(self) -> None:
        """Stop dispatching and cancel only process-owned active HTTP streams."""

        self._stopping = True
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        active = list(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.clear()

    async def _scheduler_loop(self) -> None:
        """Poll durable state; SQLite leases make the loop restart-safe."""

        while not self._stopping:
            try:
                for team_id in await self.store.running_team_ids():
                    await self._dispatch_team(team_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Team scheduler iteration failed")
            await asyncio.sleep(0.5)

    async def _dispatch_team(self, team_id: str) -> None:
        # A Team has its own maxParallel and the global semaphore bounds total
        # provider pressure. Claiming happens before spawning so two loops can
        # never dispatch the same task.
        while not self._stopping:
            claimed = await self.store.claim_next_task(team_id, self._task_lease_seconds)
            if claimed is None:
                return
            task_id = claimed["task"]["id"]
            run_task = asyncio.create_task(
                self._run_claimed(claimed), name=f"team-task-{task_id}"
            )
            self._active[task_id] = run_task
            run_task.add_done_callback(lambda _done, key=task_id: self._active.pop(key, None))

    async def _run_claimed(self, claimed: dict[str, Any]) -> None:
        team = claimed["team"]
        task = claimed["task"]
        agent = claimed["agent"]
        team_id = str(team["id"])
        task_id = str(task["id"])
        agent_id = str(agent["id"])
        run_id = str(claimed["runId"])
        async with self._semaphore:
            try:
                workspace = Path(agent["workspaceDir"])
                await self._prepare_workspace(team_id, agent_id, workspace)
                context = await self.store.task_context(team_id, task_id)
                mcp_ids = list(agent["capabilities"].get("mcpServerIds", []))
                skill_ids = list(agent["capabilities"].get("skillIds", []))
                mcp_servers, skills = self._runtime_catalog.selected_runtime(mcp_ids, skill_ids)
                prompt = self._task_prompt(context, agent)
                request_id = f"team-{uuid.uuid4().hex}"
                await self.store.append_event(team_id, "run.started", {
                    "taskId": task_id,
                    "agentId": agent_id,
                    "runId": run_id,
                    "agentKind": agent["agentKind"],
                })
                text = await self._stream_agent(
                    team_id=team_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    request_id=request_id,
                    prompt=prompt,
                    agent=agent,
                    mcp_servers=mcp_servers,
                    skills=skills,
                    workspace=workspace,
                )
                if not text.strip():
                    text = "Agent 已完成运行，但没有返回可保存的文本成果。"
                artifact_id = await self.store.complete_task(
                    team_id, task_id, agent_id, text.strip()
                )
                await self.store.send_message(
                    team_id,
                    agent_id,
                    str(team.get("supervisor_agent_id") or "broadcast"),
                    "artifact_ready",
                    f"任务《{task['title']}》已完成，成果见 Artifact。",
                    [artifact_id],
                )
            except asyncio.CancelledError:
                await self.store.fail_task(
                    team_id, task_id, agent_id, "Team Runtime stopped during this run"
                )
                raise
            except Exception as exc:
                logger.exception("Team task failed: team=%s task=%s", team_id, task_id)
                await self.store.fail_task(team_id, task_id, agent_id, str(exc))

    async def _prepare_workspace(self, team_id: str, agent_id: str, workspace: Path) -> None:
        """Create a detached worktree when possible, otherwise an isolated directory."""

        if workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)
            return
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if (self._project_root / ".git").exists():
            process = await asyncio.create_subprocess_exec(
                "git", "worktree", "add", "--detach", str(workspace), "HEAD",
                cwd=str(self._project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                await self.store.append_event(team_id, "workspace.created", {
                    "agentId": agent_id,
                    "mode": "worktree",
                    "path": str(workspace),
                })
                return
            logger.warning(
                "Worktree creation failed for %s: %s",
                agent_id,
                stderr.decode("utf-8", errors="replace")[:1000],
            )
        workspace.mkdir(parents=True, exist_ok=True)
        await self.store.append_event(team_id, "workspace.created", {
            "agentId": agent_id,
            "mode": "directory",
            "path": str(workspace),
        })

    def _task_prompt(self, context: dict[str, Any], agent: dict[str, Any]) -> str:
        team = context["team"]
        task = context["task"]
        artifact_parts = []
        for artifact in context["artifacts"]:
            content = str(artifact.get("content") or "")
            artifact_parts.append(
                f"### {artifact['uri']} — {artifact['title']}\n{content[:60_000]}"
            )
        mailbox_parts = [
            f"- {message['senderId']}: {message['content']}"
            for message in context["mailbox"]
        ]
        return "\n\n".join(
            part
            for part in [
                "[K Agent Team Runtime]",
                f"团队目标：{team['goal']}",
                f"你的身份：{agent['name']} / {agent['role']}",
                f"职责边界：{agent['responsibility'] or '完成当前被分配任务'}",
                f"当前任务：{task['title']}\n{task['description']}",
                (
                    "协作约束：只处理当前任务；基于给出的 Artifact，而不是猜测其他 Agent 的工作；"
                    "普通工具失败时阅读错误并自行修复；最终输出应是可以直接保存为 Artifact 的完整成果。"
                ),
                "依赖 Artifact：\n" + "\n\n".join(artifact_parts) if artifact_parts else "",
                "Mailbox：\n" + "\n".join(mailbox_parts) if mailbox_parts else "",
            ]
            if part
        )

    async def _stream_agent(
        self,
        *,
        team_id: str,
        task_id: str,
        agent_id: str,
        run_id: str,
        request_id: str,
        prompt: str,
        agent: dict[str, Any],
        mcp_servers: list[dict[str, Any]],
        skills: list[dict[str, Any]],
        workspace: Path,
    ) -> str:
        chunks: list[str] = []
        pending_output: list[str] = []
        pending_reasoning: list[str] = []
        last_activity_flush = time.monotonic()
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "threadId": f"{team_id}:{agent_id}",
            "runId": run_id,
            "messages": [{
                "id": f"message_{uuid.uuid4().hex}",
                "role": "user",
                "content": prompt,
                "createdAt": created_at,
            }],
            "modelId": agent.get("modelId"),
            "mcpServers": mcp_servers,
            "skills": skills,
            "reasoningEffort": agent.get("reasoningEffort"),
            "attachments": [],
            "agentKind": agent["agentKind"],
            "agentOptions": {"cliSessionMode": "ephemeral"},
            "teamId": team_id,
            "taskId": task_id,
            "teamAgentId": agent_id,
            "attemptId": run_id,
            "workspaceDir": str(workspace),
        }

        async def flush_stream_activity(*, force: bool = False) -> None:
            """Persist readable batches, not one SQLite event per provider token."""

            nonlocal last_activity_flush
            pending_size = sum(map(len, pending_output)) + sum(map(len, pending_reasoning))
            if not force and pending_size < 800 and time.monotonic() - last_activity_flush < 0.4:
                return
            for event_type, pending in (
                ("run.output", pending_output),
                ("run.reasoning", pending_reasoning),
            ):
                content = "".join(pending)
                pending.clear()
                # A single provider delta can be unexpectedly large. Bound each
                # durable envelope while preserving its complete ordered text.
                for offset in range(0, len(content), 4000):
                    await self.store.append_event(team_id, event_type, {
                        "taskId": task_id,
                        "agentId": agent_id,
                        "runId": run_id,
                        "delta": content[offset:offset + 4000],
                    })
            last_activity_flush = time.monotonic()

        async for event in self._backend_client.stream(payload, request_id):
            event_type = str(event.get("type") or "")
            if event_type == "TEXT_MESSAGE_CONTENT":
                delta = event.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
                    pending_output.append(delta)
                    await flush_stream_activity()
            elif event_type in {"REASONING_MESSAGE_CONTENT", "THINKING_TEXT_MESSAGE_CONTENT"}:
                delta = event.get("delta")
                if isinstance(delta, str):
                    pending_reasoning.append(delta)
                    await flush_stream_activity()
            elif event_type == "RUN_ERROR":
                await flush_stream_activity(force=True)
                raise RuntimeError(str(event.get("message") or "Agent run failed"))
            elif event_type == "CUSTOM" and str(event.get("name") or "") in {
                "approval_request", "approval_resolved",
            }:
                await flush_stream_activity(force=True)
                value = event.get("value") if isinstance(event.get("value"), dict) else {}
                approval_event_type = (
                    "approval.requested"
                    if event.get("name") == "approval_request"
                    else "approval.resolved"
                )
                await self.store.record_approval(
                    team_id,
                    task_id,
                    agent_id,
                    approval_event_type,
                    {
                        **self._compact_event(value),
                        "taskId": task_id,
                        "agentId": agent_id,
                        "runId": run_id,
                    },
                )
            elif event_type in {
                "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END",
                "TOOL_CALL_RESULT", "CUSTOM",
            }:
                await flush_stream_activity(force=True)
                await self.store.append_event(team_id, "run.activity", {
                    "taskId": task_id,
                    "agentId": agent_id,
                    "runId": run_id,
                    "event": self._compact_event(event),
                })
        await flush_stream_activity(force=True)
        return "".join(chunks)

    @staticmethod
    def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
        """Bound persisted activity so tool output cannot inflate Team replay."""

        compact = dict(event)
        if isinstance(compact.get("content"), str):
            compact["content"] = compact["content"][:4000]
        if isinstance(compact.get("delta"), str):
            compact["delta"] = compact["delta"][:1000]
        # Ensure provider-specific nested values remain JSON serializable.
        return json.loads(json.dumps(compact, ensure_ascii=False, default=str))
