"""Background scheduler that turns durable Team tasks into isolated Agent runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from access_layer.agent_backend_client import AgentBackendClient
from access_layer.catalog import RuntimeCatalog
from access_layer.teams.models import SupervisorDecision
from access_layer.teams.store import TeamStore
from backend.home import (
    ensure_shared_runtime,
    link_shared_runtime,
    public_home_relative_path,
    resolve_managed_path,
    shared_runtime_tool_env,
    to_managed_path,
)


logger = logging.getLogger("k_agent.access.team_runtime")

# Deliverable trees are source snapshots. Dependency installs and tool caches
# must not ship with Artifacts. Built output dirs like dist/ are allowed when
# that is the intentional deliverable. Shared tooling lives under `.runtime/`
# (sibling of output/), never inside the published tree.
_ARTIFACT_COPY_IGNORE = shutil.ignore_patterns(
    ".team-input",
    ".runtime",
    "node_modules",
    ".pnpm-store",
    ".yarn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".turbo",
    ".next",
    ".nuxt",
    ".cache",
    "coverage",
    ".git",
)


class TeamRuntime:
    """Dispatch dependency-ready tasks while preserving Team state in SQLite."""

    def __init__(
        self,
        *,
        store: TeamStore,
        backend_client: AgentBackendClient,
        runtime_catalog: RuntimeCatalog,
        max_active_runs: int = 8,
        task_lease_seconds: int = 120,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self._backend_client = backend_client
        self._runtime_catalog = runtime_catalog
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
        await self._reconcile_artifact_publications()
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
        supervisor_key = f"supervisor:{team_id}"
        if supervisor_key in self._active:
            return
        if supervisor_key not in self._active:
            control = await self.store.claim_supervisor_job(
                team_id, self._task_lease_seconds
            )
            if control is not None:
                control_task = asyncio.create_task(
                    self._run_supervisor(control), name=f"team-{supervisor_key}"
                )
                self._active[supervisor_key] = control_task
                control_task.add_done_callback(
                    lambda _done, key=supervisor_key: self._active.pop(key, None)
                )
                return
        if await self.store.has_supervisor_work(team_id):
            return

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
        task_dir = self.store.database_path.parent / team_id / "tasks" / task_id
        async with self._semaphore:
            try:
                context = await self.store.task_context(team_id, task_id)
                runtime = ensure_shared_runtime()
                # cwd is the task bundle. Deliverables stay under output/; the
                # project-wide Node tooling is linked at .runtime.
                workspace = task_dir
                self._prepare_task_directory(
                    task_dir, context, agent, run_id, runtime=runtime
                )
                mcp_ids = list(agent["capabilities"].get("mcpServerIds", []))
                skill_ids = list(agent["capabilities"].get("skillIds", []))
                mcp_servers, skills = self._runtime_catalog.selected_runtime(mcp_ids, skill_ids)
                prompt = self._task_prompt(context, agent, runtime=runtime)
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
                    tool_env=shared_runtime_tool_env(runtime),
                    run_log=task_dir / "logs" / f"{run_id}.ndjson",
                )
                if not text.strip():
                    text = "Agent 已完成运行，但没有返回可保存的文本成果。"
                artifact_id = await self.store.submit_task(
                    team_id, task_id, agent_id, text.strip()
                )
                self._write_task_result(task_dir, artifact_id, text.strip())
                await self.store.send_message(
                    team_id,
                    agent_id,
                    str(team.get("supervisor_agent_id") or "broadcast"),
                    "artifact_ready",
                    f"任务《{task['title']}》已提交，成果见待验收 Artifact。",
                    [artifact_id],
                )
            except asyncio.CancelledError:
                self._update_manifest(task_dir, status="pending", error="Team Runtime stopped during this run")
                await self.store.interrupt_task(
                    team_id, task_id, agent_id, "Team Runtime stopped during this run"
                )
                raise
            except Exception as exc:
                logger.exception("Team task failed: team=%s task=%s", team_id, task_id)
                self._update_manifest(task_dir, status="failed", error=str(exc))
                await self.store.fail_task(team_id, task_id, agent_id, str(exc))

    async def _run_supervisor(self, control: dict[str, Any]) -> None:
        """Wake the manager only at a durable scheduling boundary."""

        team = control["team"]
        job = control["job"]
        supervisor = control["supervisor"]
        team_id = str(team["id"])
        job_id = str(job["id"])
        supervisor_id = str(supervisor["id"])
        run_id = str(control["runId"])
        control_dir = self.store.database_path.parent / team_id / "supervisor" / job_id
        staged_publications: list[tuple[str, Path, Path]] = []
        async with self._semaphore:
            try:
                context = await self.store.supervisor_context(team_id, job_id)
                control_dir.mkdir(parents=True, exist_ok=True)
                prompt = self._supervisor_prompt(
                    context,
                    trigger_type=str(job["triggerType"]),
                    trigger_payload=control.get("triggerPayload", {}),
                    previous_error=job.get("error"),
                )
                text = await self._stream_agent(
                    team_id=team_id,
                    task_id=job_id,
                    agent_id=supervisor_id,
                    run_id=run_id,
                    request_id=f"team-supervisor-{uuid.uuid4().hex}",
                    prompt=prompt,
                    agent=supervisor,
                    # Scheduling is a control-plane operation. It must not gain
                    # side-effecting MCP/Skill authority from a worker profile.
                    mcp_servers=[],
                    skills=[],
                    workspace=control_dir,
                    run_log=control_dir / "run.ndjson",
                )
                decision = self._parse_supervisor_decision(text)
                (control_dir / "decision.json").write_text(
                    decision.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
                )
                staged_publications = self._stage_decision_artifacts(
                    context["team"], decision, job_id
                )
                await self.store.apply_supervisor_decision(
                    team_id, job_id, supervisor_id, decision
                )
                await self._finalize_artifact_publications(
                    team_id, staged_publications
                )
                try:
                    self._sync_supervisor_manifests(team_id, decision)
                except Exception:
                    # SQLite is authoritative. A readable task-local mirror
                    # failing after commit must not replay control mutations.
                    logger.exception(
                        "Could not update task manifest after supervisor commit: team=%s job=%s",
                        team_id,
                        job_id,
                    )
            except asyncio.CancelledError:
                await self.store.interrupt_supervisor_job(
                    team_id, job_id, supervisor_id, "Team Runtime stopped during supervisor decision"
                )
                raise
            except Exception as exc:
                self._discard_staged_publications(staged_publications)
                logger.exception(
                    "Supervisor decision failed: team=%s job=%s", team_id, job_id
                )
                await self.store.fail_supervisor_job(
                    team_id, job_id, supervisor_id, str(exc)
                )

    def _stage_decision_artifacts(
        self,
        team: dict[str, Any],
        decision: SupervisorDecision,
        job_id: str,
    ) -> list[tuple[str, Path, Path]]:
        """Copy candidate files into the Team workspace before accepting state."""

        artifacts = list(team.get("artifacts") or [])
        staged: list[tuple[str, Path, Path]] = []
        for action in decision.actions:
            if action.type != "accept_submission" or not action.task_id:
                continue
            artifact = next(
                (
                    item
                    for item in artifacts
                    if item["taskId"] == action.task_id
                    and item["status"] == "pending_review"
                    and (action.artifact_id is None or item["id"] == action.artifact_id)
                ),
                None,
            )
            if artifact is None:
                raise ValueError(f"No pending Artifact to publish for task {action.task_id}")
            staged.append(self._stage_artifact(team, artifact, job_id))
        return staged

    def _stage_artifact(
        self, team: dict[str, Any], artifact: dict[str, Any], staging_key: str
    ) -> tuple[str, Path, Path]:
        """Create a complete same-filesystem staging bundle for atomic promotion."""

        workspace = self._ensure_team_workspace(team)
        artifact_id = str(artifact["id"])
        task_id = str(artifact["taskId"])
        staging = workspace / ".k_agent-staging" / staging_key / artifact_id
        final_path = workspace / "artifacts" / task_id / artifact_id
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        source_output = (
            self.store.database_path.parent / str(team["id"]) / "tasks" / task_id / "output"
        )
        if source_output.is_dir():
            shutil.copytree(
                source_output,
                staging / "files",
                dirs_exist_ok=True,
                ignore=_ARTIFACT_COPY_IGNORE,
            )
        (staging / "result.md").write_text(
            str(artifact.get("content") or ""), encoding="utf-8"
        )
        (staging / "artifact.json").write_text(
            json.dumps(
                {
                    "artifactId": artifact_id,
                    "taskId": task_id,
                    "teamId": team["id"],
                    "title": artifact["title"],
                    "kind": artifact["kind"],
                    "sha256": artifact["sha256"],
                    "uri": artifact["uri"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return artifact_id, staging, final_path

    @staticmethod
    def _ensure_team_workspace(team: dict[str, Any]) -> Path:
        """Create/verify the Team marker before publishing any accepted files."""

        workspace = resolve_managed_path(team["workspaceDir"])
        workspace.mkdir(parents=True, exist_ok=True)
        marker_path = workspace / ".k_agent-team.json"
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("teamId") != team["id"]:
                raise ValueError("Team workspace marker belongs to another Team")
        else:
            marker_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "teamId": team["id"],
                        "teamName": team["name"],
                        "artifactDirectory": "artifacts",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return workspace

    async def _finalize_artifact_publications(
        self, team_id: str, publications: list[tuple[str, Path, Path]]
    ) -> None:
        """Promote staged bundles before releasing the supervisor dispatch gate."""

        for artifact_id, staging, final_path in publications:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if final_path.exists():
                shutil.rmtree(staging, ignore_errors=True)
            else:
                staging.replace(final_path)
            await self.store.record_artifact_publication(
                team_id, artifact_id, final_path
            )
        self._discard_staged_publications(publications)

    @staticmethod
    def _discard_staged_publications(
        publications: list[tuple[str, Path, Path]]
    ) -> None:
        for _artifact_id, staging, _final_path in publications:
            shutil.rmtree(staging, ignore_errors=True)

    async def _reconcile_artifact_publications(self) -> None:
        """Publish accepted legacy Artifacts that predate workspace paths."""

        for summary in await self.store.list_teams():
            team = await self.store.get_team(str(summary["id"]))
            if team is None:
                continue
            try:
                self._ensure_team_workspace(team)
            except Exception:
                logger.exception("Could not initialize Team workspace: team=%s", team["id"])
                continue
            for artifact in team["artifacts"]:
                if artifact["status"] != "accepted" or artifact.get("workspacePath"):
                    continue
                staged: list[tuple[str, Path, Path]] = []
                try:
                    staged.append(
                        self._stage_artifact(team, artifact, f"reconcile-{artifact['id']}")
                    )
                    await self._finalize_artifact_publications(team["id"], staged)
                except Exception:
                    self._discard_staged_publications(staged)
                    logger.exception(
                        "Could not publish legacy Artifact: team=%s artifact=%s",
                        team["id"],
                        artifact["id"],
                    )

    def _sync_supervisor_manifests(
        self, team_id: str, decision: SupervisorDecision
    ) -> None:
        """Mirror accepted/revision state into non-authoritative task bundles."""

        tasks_root = self.store.database_path.parent / team_id / "tasks"
        for action in decision.actions:
            if not action.task_id:
                continue
            task_dir = tasks_root / action.task_id
            if action.type == "accept_submission":
                self._update_manifest(
                    task_dir,
                    status="completed",
                    artifact_id=action.artifact_id,
                )
            elif action.type == "request_revision":
                self._update_manifest(
                    task_dir,
                    status="revision_required",
                    error=action.reason,
                )

    def _prepare_task_directory(
        self,
        task_dir: Path,
        context: dict[str, Any],
        agent: dict[str, Any],
        run_id: str,
        *,
        runtime: Path | None = None,
    ) -> None:
        """Materialize one non-code task bundle without copying the repository."""

        input_dir = task_dir / "input"
        artifact_input_dir = input_dir / "artifacts"
        output_dir = task_dir / "output"
        runtime_artifact_dir = output_dir / ".team-input" / "artifacts"
        for directory in (
            artifact_input_dir,
            runtime_artifact_dir,
            task_dir / "artifacts",
            task_dir / "logs",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if runtime is not None:
            link_shared_runtime(task_dir, runtime)

        task = context["task"]
        team = context["team"]
        (input_dir / "task.json").write_text(
            json.dumps(
                {
                    "teamId": team["id"],
                    "teamGoal": team["goal"],
                    "teamWorkspace": to_managed_path(
                        team.get("workspace_dir") or team.get("workspaceDir") or "."
                    ),
                    "task": task,
                    "agent": {
                        "id": agent["id"],
                        "name": agent["name"],
                        "role": agent["role"],
                        "responsibility": agent["responsibility"],
                    },
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        (input_dir / "mailbox.json").write_text(
            json.dumps(context["mailbox"], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        for artifact in context["artifacts"]:
            artifact_id = str(artifact["id"])
            (artifact_input_dir / f"{artifact_id}.md").write_text(
                f"# {artifact['title']}\n\nURI: {artifact['uri']}\n\n{artifact['content']}",
                encoding="utf-8",
            )
            published_path = artifact.get("workspacePath")
            if published_path:
                published_dir = resolve_managed_path(str(published_path))
                if published_dir.is_dir():
                    shutil.copytree(
                        published_dir,
                        runtime_artifact_dir / artifact_id,
                        dirs_exist_ok=True,
                    )
        manifest = {
            "schemaVersion": 1,
            "teamId": team["id"],
            "taskId": task["id"],
            "runId": run_id,
            "agentId": agent["id"],
            "status": "running",
            "inputArtifacts": [artifact["uri"] for artifact in context["artifacts"]],
            "inputArtifactFiles": {
                artifact["id"]: f"output/.team-input/artifacts/{artifact['id']}"
                for artifact in context["artifacts"]
                if artifact.get("workspacePath")
            },
            "outputDirectory": "output",
            "artifactDirectory": "artifacts",
            "logFile": f"logs/{run_id}.ndjson",
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        (task_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _write_task_result(self, task_dir: Path, artifact_id: str, content: str) -> None:
        """Keep a readable submitted copy beside the authoritative SQLite Artifact."""

        artifact_dir = task_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / f"{artifact_id}.md").write_text(content, encoding="utf-8")
        self._update_manifest(task_dir, status="submitted", artifact_id=artifact_id)

    @staticmethod
    def _update_manifest(
        task_dir: Path,
        *,
        status: str,
        artifact_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update recoverable task metadata without making files the state authority."""

        manifest_path = task_dir / "manifest.json"
        if not manifest_path.exists():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = status
        manifest["updatedAt"] = datetime.now(timezone.utc).isoformat()
        if artifact_id is not None:
            manifest["artifactId"] = artifact_id
        if error is not None:
            manifest["error"] = error[:4000]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _append_run_log(path: Path, event: dict[str, Any]) -> None:
        """Append the original provider event for task-local audit and recovery."""

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _task_prompt(
        self,
        context: dict[str, Any],
        agent: dict[str, Any],
        *,
        runtime: Path | None = None,
    ) -> str:
        team = context["team"]
        task = context["task"]
        artifact_parts = []
        for artifact in context["artifacts"]:
            content = str(artifact.get("content") or "")
            artifact_parts.append(
                f"### {artifact['uri']} — {artifact['title']}\n"
                + (
                    f"已验收文件快照：output/.team-input/artifacts/{artifact['id']}\n"
                    if artifact.get("workspacePath")
                    else ""
                )
                + content[:60_000]
            )
        mailbox_parts = [
            f"- {message['senderId']}: {message['content']}"
            for message in context["mailbox"]
        ]
        runtime_hint = ""
        if runtime is not None:
            runtime_rel = public_home_relative_path(runtime) or str(runtime)
            runtime_hint = (
                f"项目共享运行时：{runtime_rel}（任务内入口为 .runtime/；"
                "整个 $K_AGENT_HOME 下所有会话与团队共用，禁止按任务重复安装）。\n"
                "CLI 全局安装（全项目只需一次）："
                "npm install -g --prefix \"$K_AGENT_SHARED_RUNTIME/node\" <pkg>\n"
                "项目依赖装到共享目录，不要装进 output/："
                "mkdir -p \"$K_AGENT_SHARED_RUNTIME/projects/<slug>\" && "
                "在该目录放置 package.json/lock 后执行 npm ci；"
                "验证构建时在该共享目录运行，或临时 ln -s 其 node_modules 到 output 下项目，"
                "提交前必须删除该符号链接。"
            )
        return "\n\n".join(
            part
            for part in [
                "[K Agent Team Runtime]",
                f"团队目标：{team['goal']}",
                f"团队工作空间：{public_home_relative_path(team.get('workspace_dir') or team.get('workspaceDir')) or 'workspace'}（只有主管验收后的产物会发布到这里）",
                f"你的身份：{agent['name']} / {agent['role']}",
                f"职责边界：{agent['responsibility'] or '完成当前被分配任务'}",
                f"当前任务：{task['title']}\n{task['description']}",
                (
                    "协作约束：只处理当前任务；基于给出的 Artifact，而不是猜测其他 Agent 的工作；"
                    "普通工具失败时阅读错误并自行修复；最终输出应是可以直接保存为 Artifact 的完整成果。"
                ),
                (
                    "交付目录约束：正式交付物只写在 output/。"
                    "禁止在 output/ 内执行 npm/pnpm/yarn/bun install 或 pip install -t .；"
                    "不要留下 node_modules、.venv、__pycache__、.next 等依赖或工具缓存；"
                    "只提交源码与 lockfile（或明确约定的构建产物本身）。"
                ),
                runtime_hint,
                "依赖 Artifact：\n" + "\n\n".join(artifact_parts) if artifact_parts else "",
                "Mailbox：\n" + "\n".join(mailbox_parts) if mailbox_parts else "",
            ]
            if part
        )

    def _supervisor_prompt(
        self,
        context: dict[str, Any],
        *,
        trigger_type: str,
        trigger_payload: dict[str, Any],
        previous_error: str | None,
    ) -> str:
        """Build a bounded scheduling prompt whose only authority is JSON actions."""

        team = context["team"]
        agents = [
            {
                "id": agent["id"],
                "name": agent["name"],
                "role": agent["role"],
                "agentKind": agent["agentKind"],
                "modelId": agent["modelId"],
                "reasoningEffort": agent["reasoningEffort"],
                "networkAccess": agent["networkAccess"],
                "responsibility": agent["responsibility"],
                "status": agent["status"],
                "capabilities": agent["capabilities"],
                "isSupervisor": agent["isSupervisor"],
            }
            for agent in team["agents"]
        ]
        tasks = [
            {
                "id": task["id"],
                "title": task["title"],
                "description": task["description"],
                "taskType": task["taskType"],
                "status": task["status"],
                "ownerAgentId": task["ownerAgentId"],
                "dependsOn": task["dependsOn"],
                "attempt": task["attempt"],
                "error": task["error"],
            }
            for task in team["tasks"]
        ]
        artifacts = [
            {
                "id": artifact["id"],
                "taskId": artifact["taskId"],
                "agentId": artifact["agentId"],
                "title": artifact["title"],
                "status": artifact["status"],
                "uri": artifact["uri"],
                "content": str(artifact["content"])[:60_000],
            }
            for artifact in team["artifacts"]
            if artifact["status"] in {"pending_review", "accepted"}
        ]
        mailbox = [
            {
                "senderId": message["senderId"],
                "recipientId": message["recipientId"],
                "messageType": message["messageType"],
                "content": message["content"],
                "artifactIds": message["artifactIds"],
            }
            for message in team["mailbox"][:30]
        ]
        state = {
            "team": {
                "id": team["id"],
                "goal": team["goal"],
                "mode": team["mode"],
                "maxParallel": team["maxParallel"],
                "workspaceDir": team["workspaceDir"],
            },
            "trigger": {"type": trigger_type, "payload": trigger_payload},
            "agents": agents,
            "tasks": tasks,
            "artifacts": artifacts,
            "mailbox": mailbox,
        }
        return "\n\n".join(
            part
            for part in [
                "[K Agent Team Supervisor Control Loop]",
                "你是团队主管。你只在任务级调度边界做决策，不复述 Artifact，也不执行成员任务。",
                (
                    "可用 action type：approve_plan、accept_submission、request_revision、"
                    "create_task、assign_task、reassign_task、request_review、ask_human、finish_team。"
                ),
                (
                    "规则：task.submitted 必须对触发 taskId 执行 accept_submission 或 request_revision；"
                    "team.started 如果还没有任务，必须先根据团队目标和成员能力生成最小、完整的任务 DAG；"
                    "先识别必须串行的前置输入，再识别可以独立并行的工作，并根据 role、responsibility、capabilities、"
                    "agentKind、modelId 和 networkAccess 选择承接者。不要为了让每个成员都有工作而创建任务，允许成员暂时空闲。"
                    "每个 create_task 必须提供唯一 taskKey，"
                    "同一决策内的 dependsOn 使用 taskKey，引用已有任务时使用 taskId。只有真正需要上游 Artifact 的任务才设置依赖，"
                    "没有依赖的任务会在 maxParallel 范围内并行；如果当前已有未分配任务，使用 assign_task 明确负责人；"
                    "系统会把已验收 Artifact 原样提供给下一个 Agent；只有全部任务完成后才能 finish_team。"
                ),
                (
                    "只输出一个 JSON 对象，不要 Markdown 代码块或额外文字。格式："
                    '{"summary":"决策摘要","actions":[{"type":"...","taskKey":"research",'
                    '"taskId":"...",'
                    '"artifactId":"...","assigneeAgentId":"...","reviewerAgentId":"...",'
                    '"title":"...","description":"...","dependsOn":[],"priority":50,"reason":"..."}]}'
                ),
                f"上一次决策校验错误：{previous_error}" if previous_error else "",
                "当前控制状态：\n" + json.dumps(state, ensure_ascii=False, default=str),
            ]
            if part
        )

    @staticmethod
    def _parse_supervisor_decision(text: str) -> SupervisorDecision:
        """Accept plain or fenced JSON while rejecting surrounding prose/actions."""

        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[-1].strip().startswith("```"):
                candidate = "\n".join(lines[1:-1]).strip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError("Supervisor must return one valid JSON decision object") from exc
        return SupervisorDecision.model_validate(payload)

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
        tool_env: dict[str, str] | None = None,
        run_log: Path | None = None,
    ) -> str:
        chunks: list[str] = []
        pending_output: list[str] = []
        pending_reasoning: list[str] = []
        last_activity_flush = time.monotonic()
        created_at = datetime.now(timezone.utc).isoformat()
        agent_options: dict[str, Any] = {
            "cliSessionMode": "ephemeral",
            **(
                {"networkAccess": agent["networkAccess"]}
                if isinstance(agent.get("networkAccess"), bool)
                else {}
            ),
        }
        if tool_env:
            agent_options["toolEnv"] = tool_env
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
            "agentOptions": agent_options,
            "teamId": team_id,
            "taskId": task_id,
            "teamAgentId": agent_id,
            "attemptId": run_id,
            "workspaceDir": to_managed_path(workspace),
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
            if run_log is not None:
                self._append_run_log(run_log, event)
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
