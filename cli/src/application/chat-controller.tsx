import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "ink";
import type { AccessLayerClient } from "../client/access-layer-client.js";
import type { CliRuntimeConfig } from "../config/index.js";
import { createResumeInput, createRunInput } from "./run-input.js";
import {
  applyMcpServersToRuntime,
  parseMcpSlashArgs,
  sanitizeMcpServersForSave,
  setMcpEnabled,
  toolsFromCapabilities,
} from "./mcp-config.js";
import {
  applySkillsToRuntime,
  parseSkillSlashArgs,
  sanitizeSkillsForSave,
  setSkillEnabled,
} from "./skill-config.js";
import {
  applyRuntimeToSummary,
  choicesFromAgents,
  choicesFromModels,
  selectAgent,
  selectModel,
  selectPermission,
  parseMenuOrSetArgs,
} from "./runtime-switch.js";
import { appendUserPrompt, emptyTimeline, pendingApproval, projectEvent, timelineFromSession, type TimelineState } from "./event-projector.js";
import { consumeRun } from "./run-lifecycle.js";
import { emptyViewModel } from "./view-model.js";
import { TerminalPage, type TerminalPageAction, type TerminalPageViewModel } from "../terminal-page/index.js";
import type { AgUiRunInput, ApprovalActivity } from "../protocol/index.js";

import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { DEFAULT_PREFERENCES, readPreferences, savePreferences } from "./preferences.js";
import { applyTerminalPreferences } from "../terminal-page/design.js";
import { SessionPtyHub } from "./session-pty.js";
import { WORKBENCH_OPERATIONS, operationRequest } from "./workbench-catalog.js";
import { copyText, readAttachment, readPins, togglePin } from "./workbench-local.js";

interface ActiveRun {
  runId: string;
  controller: AbortController;
}

export function ChatController({ client, initialConfig, initialSessionId, startAt = "home" }: {
  client: AccessLayerClient;
  initialConfig: CliRuntimeConfig;
  initialSessionId?: string | undefined;
  startAt?: "home" | "chat";
}): React.ReactElement {
  const { exit, suspendTerminal } = useApp();
  const [config, setConfig] = useState(initialConfig);
  const configRef = useRef(config);
  configRef.current = config;
  const [model, setModel] = useState<TerminalPageViewModel>(() => ({ ...emptyViewModel(initialConfig), surface: startAt }));
  const preferencesRef = useRef(structuredClone(DEFAULT_PREFERENCES));
  const runtimeRef = useRef(model.runtime);
  runtimeRef.current = model.runtime;
  const ptyHubRef = useRef<SessionPtyHub | undefined>(undefined);
  ptyHubRef.current ??= new SessionPtyHub(client.endpoint);
  const workbenchWatchRef = useRef<{ operationId: string; values: Record<string, string> } | undefined>(undefined);
  const activeSessionRef = useRef<string | undefined>(initialSessionId);
  const activeProjectRef = useRef<string | undefined>(undefined);
  activeProjectRef.current = model.activeProjectId;
  const timelineCacheRef = useRef(new Map<string, TimelineState>());
  const runsRef = useRef(new Map<string, ActiveRun>());

  useEffect(() => {
    let active = true;
    void readPreferences().then((preferences) => {
      if (!active) return;
      preferencesRef.current = preferences;
      applyTerminalPreferences(preferences.theme, preferences.fontSizeDelta);
      setModel((value) => ({ ...value, preferences }));
    }).catch((error) => { if (active) setModel((value) => ({ ...value, notice: `偏好读取失败：${errorMessage(error)}` })); });
    return () => { active = false; };
  }, []);

  const loadSession = useCallback(async (sessionId: string): Promise<void> => {
    activeSessionRef.current = sessionId;
    const cached = timelineCacheRef.current.get(sessionId);
    setModel((value) => ({
      ...value,
      environment: undefined,
      workspace: undefined,
      workspaceFile: undefined,
      loading: true,
      surface: "chat",
      activeSessionId: sessionId,
      // 换会话时先清掉上一份时间线。没有缓存就等这次加载，避免旧对话留在新会话上。
      timeline: cached ?? (value.activeSessionId === sessionId ? value.timeline : emptyTimeline()),
      error: undefined,
    }));
    try {
      const session = await client.getSession(sessionId);
      const timeline = runsRef.current.has(sessionId) ? timelineCacheRef.current.get(sessionId) ?? timelineFromSession(session) : timelineFromSession(session);
      timelineCacheRef.current.set(sessionId, timeline);
      if (activeSessionRef.current === sessionId) {
        const caps = session.capabilities;
        if (caps) {
          const next = { ...configRef.current, mcpServerIds: caps.mcpServerIds, skillIds: caps.skillIds, ...(caps.permissionMode ? { permissionMode: caps.permissionMode } : {}) };
          configRef.current = next;
          setConfig(next);
        }
        setModel((value) => ({ ...value, loading: false, timeline, environment: session.environment ?? null, activeProjectId: value.sessions.find((item) => item.id === sessionId)?.projectId || value.activeProjectId, activeSessionTitle: value.sessions.find((item) => item.id === sessionId)?.title, runtime: caps ? { ...applyRuntimeToSummary(value.runtime, configRef.current), selectedMcpIds: caps.mcpServerIds, selectedSkillIds: caps.skillIds } : value.runtime, error: undefined }));
      }
    } catch (error) {
      if (activeSessionRef.current === sessionId) {
        setModel((value) => ({ ...value, loading: false, error: errorMessage(error) }));
      }
    }
  }, [client]);

  const refreshMcp = useCallback(async (notice?: string): Promise<void> => {
    try {
      const snapshot = await refreshMcpSnapshot(client);
      const nextConfig = { ...configRef.current, mcpServerIds: configRef.current.mcpServerIds.filter((id) => snapshot.servers.some((server) => server.id === id && server.enabled)) };
      configRef.current = nextConfig; setConfig(nextConfig);
      setModel((value) => ({
        ...value,
        mcpBusy: false,
        ...(notice ? { notice } : {}),
        runtime: applyMcpServersToRuntime(applyRuntimeToSummary(value.runtime, nextConfig), snapshot.servers, snapshot.tools),
      }));
    } catch (error) {
      setModel((value) => ({ ...value, mcpBusy: false, error: errorMessage(error) }));
    }
  }, [client]);

  const mutateSkills = useCallback(async (work: (access: AccessLayerClient) => Promise<string>): Promise<void> => {
    setModel((value) => ({ ...value, mcpBusy: true, error: undefined }));
    try {
      const notice = await work(client);
      const payload = await client.getSkillsConfig();
      const nextConfig = { ...configRef.current, skillIds: configRef.current.skillIds.filter((id) => payload.skills.some((skill) => skill.id === id && skill.enabled)) };
      configRef.current = nextConfig; setConfig(nextConfig);
      setModel((value) => ({
        ...value,
        mcpBusy: false,
        notice,
        runtime: applySkillsToRuntime(applyRuntimeToSummary(value.runtime, nextConfig), payload.skills),
      }));
    } catch (error) {
      setModel((value) => ({ ...value, mcpBusy: false, error: errorMessage(error) }));
    }
  }, [client]);

  const applyCliRuntime = useCallback((nextConfig: CliRuntimeConfig, notice: string): void => {
    if (nextConfig.agentKind !== configRef.current.agentKind) {
      const runtime = runtimeRef.current;
      const choices = nextConfig.agentKind === "k_agent" ? runtime.modelCatalog ?? runtime.models : runtime.agents.find((item) => item.id === nextConfig.agentKind)?.models ?? [];
      if (!choices.some((item) => item.id === nextConfig.modelId)) nextConfig = { ...nextConfig, modelId: choices.find((item) => item.enabled)?.id ?? "" };
    }
    configRef.current = nextConfig;
    setConfig(nextConfig);
    setModel((value) => ({
      ...value,
      notice,
      error: undefined,
      runtime: applyRuntimeToSummary(value.runtime, nextConfig),
    }));
  }, []);

  const mutateMcp = useCallback(async (work: (access: AccessLayerClient) => Promise<string>): Promise<void> => {
    setModel((value) => ({ ...value, mcpBusy: true, error: undefined }));
    try {
      const notice = await work(client);
      const snapshot = await refreshMcpSnapshot(client);
      const nextConfig = { ...configRef.current, mcpServerIds: configRef.current.mcpServerIds.filter((id) => snapshot.servers.some((server) => server.id === id && server.enabled)) };
      configRef.current = nextConfig; setConfig(nextConfig);
      setModel((value) => ({
        ...value,
        mcpBusy: false,
        notice,
        runtime: applyMcpServersToRuntime(applyRuntimeToSummary(value.runtime, nextConfig), snapshot.servers, snapshot.tools),
      }));
    } catch (error) {
      setModel((value) => ({ ...value, mcpBusy: false, error: errorMessage(error) }));
    }
  }, [client]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const [health, sessions, catalog, agents, models, teams, automations, projects, pins] = await Promise.all([
          client.health(),
          client.listSessions(),
          client.catalog(),
          client.agents(),
          client.models(),
          client.listTeams().catch(() => []),
          client.listScheduledTasks().catch(() => []),
          client.listProjects().catch(() => []),
          readPins().catch(() => []),
        ]);
        if (!active) return;
        const selectedAgent = agents.agents.find((agent) => agent.kind === config.agentKind);
        const selectedModel = config.modelId ?? (config.agentKind === "k_agent" ? models.find((item) => item.enabled)?.id : selectedAgent?.defaultModelId ?? selectedAgent?.models?.[0]?.id);
        const nextConfig = selectedModel ? { ...config, modelId: selectedModel } : config;
        setConfig(nextConfig);
        const modelChoices = choicesFromModels(models);
        const agentChoices = choicesFromAgents(agents.agents);
        setModel((value) => ({
          ...value,
          connected: health.ok,
          loading: false,
          health,
          sessions,
          projects,
          pinnedProjectIds: pins,
          teams,
          automations,
          runtime: {
            ...value.runtime,
            modelId: selectedModel ?? "",
            models: config.agentKind === "k_agent" ? modelChoices : agentChoices.find((agent) => agent.id === config.agentKind)?.models ?? [],
            modelCatalog: modelChoices,
            agents: agentChoices,
            mcpServers: catalog.mcpServers.map((item) => ({
              id: item.id,
              name: item.name,
              enabled: item.enabled,
            })),
            skills: catalog.skills.map((item) => ({
              id: item.id,
              name: item.name,
              enabled: item.enabled,
            })),
            selectedMcpIds: [...nextConfig.mcpServerIds],
            selectedSkillIds: [...nextConfig.skillIds],
            mcpCount: nextConfig.mcpServerIds.length,
            skillCount: nextConfig.skillIds.length,
          },
          doctorLines: doctorSummary(health, agents.agents.filter((item) => item.available).map((item) => item.kind), models.filter((item) => item.enabled).length),
        }));
        if (initialSessionId) await loadSession(initialSessionId);
      } catch (error) {
        if (active) setModel((value) => ({ ...value, loading: false, connected: false, error: errorMessage(error) }));
      }
    })();
    return () => {
      active = false;
      // 组件卸载只断开本地 SSE，不发送 stop/cancel；进程退出不能伪装成生命周期命令。
      for (const run of runsRef.current.values()) run.controller.abort();
      ptyHubRef.current?.closeAll();
    };
  }, [client, initialSessionId, loadSession]);

  const refreshSessions = useCallback(async (): Promise<void> => {
    try {
      const sessions = await client.listSessions();
      setModel((value) => ({ ...value, sessions }));
    } catch {
      // run 主结果已到达时，列表刷新失败只保留当前页面，不把成功 run 改写成失败。
    }
  }, [client]);

  const startRun = useCallback(async (sessionId: string, input: AgUiRunInput): Promise<void> => {
    if (runsRef.current.has(sessionId)) {
      setModel((value) => ({ ...value, notice: "该 Session 已有运行中的任务" }));
      return;
    }
    const controller = new AbortController();
    runsRef.current.set(sessionId, { runId: input.runId, controller });
    try {
      await consumeRun(client, input, {
        signal: controller.signal,
        onEvent: (event) => {
          const previous = timelineCacheRef.current.get(sessionId) ?? emptyTimeline();
          const next = projectEvent(previous, event);
          timelineCacheRef.current.set(sessionId, next);
          if (activeSessionRef.current === sessionId) {
            setModel((value) => ({ ...value, timeline: next, notice: undefined, error: undefined }));
          }
        },
      });
    } catch (error) {
      if (!controller.signal.aborted && activeSessionRef.current === sessionId) {
        const current = timelineCacheRef.current.get(sessionId) ?? emptyTimeline();
        const failed = input.resume?.length ? updateApprovalStatus(current, input.resume[0]!.interruptId, "resume_failed") : projectEvent(current, { type: "RUN_ERROR", message: `事件流中断：${errorMessage(error)}` });
        timelineCacheRef.current.set(sessionId, failed);
        setModel((value) => ({ ...value, timeline: failed, error: `事件流中断：${errorMessage(error)}` }));
      }
    } finally {
      if (runsRef.current.get(sessionId)?.runId === input.runId) runsRef.current.delete(sessionId);
      await refreshSessions();
    }
  }, [client, refreshSessions]);

  const settleApproval = useCallback((
    sessionId: string,
    approvalId: string,
    action: "approve" | "deny" | "cancel" | "answer",
  ): void => {
    const current = timelineCacheRef.current.get(sessionId);
    const item = current?.items.find((entry) => entry.kind === "approval" && entry.id === approvalId);
    // 流里已经带回更确定的结果（resume_failed 等）时不覆盖它。
    if (!current || item?.kind !== "approval" || item.approval.status !== "submitting") return;
    const settled = updateApprovalStatus(current, approvalId, RESUME_RESULT_STATUS[action]);
    timelineCacheRef.current.set(sessionId, settled);
    if (activeSessionRef.current === sessionId) setModel((value) => ({ ...value, timeline: settled }));
  }, []);

  const handleAction = useCallback(async (action: TerminalPageAction): Promise<void> => {
    if (action.type === "open_workbench" || action.type === "close_workbench") {
      setModel((value) => ({ ...value, workbenchOpen: action.type === "open_workbench" }));
      return;
    }
    if (action.type === "workbench_execute" || action.type === "workbench_load") {
      workbenchWatchRef.current = undefined;
      const operation = WORKBENCH_OPERATIONS.find((item) => item.id === action.operationId);
      if (!operation) return;
      setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: true } }));
      try {
        if (action.type === "workbench_load") {
          if (operation.local === "preferences") {
            const preferences = preferencesRef.current;
            const formValues = Object.fromEntries(operation.fields.map((field) => [field.key, String((preferences as unknown as Record<string, unknown>)[field.key] ?? field.value ?? "")]));
            setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: false, formValues } })); return;
          }
          if (operation.local === "runtime") {
            const formValues = Object.fromEntries(operation.fields.map((field) => [field.key, configRef.current[field.key as keyof CliRuntimeConfig] === undefined ? "" : field.kind === "json" ? JSON.stringify(configRef.current[field.key as keyof CliRuntimeConfig]) : String(configRef.current[field.key as keyof CliRuntimeConfig])]));
            setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: false, formValues } }));
            return;
          }
          if (!operation.editSource) throw new Error("此操作没有可读取的编辑源");
          const source = { ...operation, path: operation.editSource, fields: operation.fields.filter((field) => field.location === "path") };
          const request = operationRequest(source, action.values);
          const current = await client.workbenchRequest(request.path) as Record<string, unknown>;
          const fields = operation.editKey ? { [operation.editKey]: current[operation.editKey] } : current;
          const formValues = Object.fromEntries(operation.fields.filter((field) => field.location === "body" && fields[field.key] !== undefined && fields[field.key] !== null).map((field) => [field.key, field.kind === "json" ? JSON.stringify(fields[field.key], null, 2) : String(fields[field.key])]));
          setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: false, formValues, result: current } }));
          return;
        }
        const request = operationRequest(operation, action.values);
        let result: unknown;
        if (operation.local === "preferences") {
          const old = preferencesRef.current;
          const next = { ...old, ...request.body };
          const preferences = await savePreferences(next);
          preferencesRef.current = preferences;
          applyTerminalPreferences(preferences.theme, preferences.fontSizeDelta);
          setModel((value) => ({ ...value, preferences }));
          result = preferences;
        } else if (operation.local === "timeline") {
          result = model.timeline;
        } else if (operation.local === "runtime") {
          const next = { ...configRef.current, ...request.body } as CliRuntimeConfig;
          if (!action.values.networkAccess) delete next.networkAccess;
          if (!action.values.resumeSessionId) delete next.resumeSessionId;
          applyCliRuntime(next, "运行选项已更新，下一轮生效");
          result = next;
        } else if (operation.local === "attachment") {
          if (operation.id === "attachments-clear") {
            setModel((value) => ({ ...value, attachments: [] }));
            result = "已清除附件";
          } else {
            if ((model.attachments?.length ?? 0) >= 4) throw new Error("每轮最多 4 个附件");
            const attachment = await readAttachment(action.values.file!);
            setModel((value) => ({ ...value, attachments: [...(value.attachments ?? []), attachment] }));
            result = { name: attachment.name, type: attachment.type, status: "等待下一次发送" };
          }
        } else if (operation.local === "import") {
          const file = action.values.file!;
          result = await client.importSkill(basename(file), await readFile(file));
        } else if (operation.local === "copy") {
          const item = action.values.messageId ? model.timeline.items.find((entry) => entry.id === action.values.messageId) : [...model.timeline.items].reverse().find((entry) => entry.kind === "text");
          if (!item) throw new Error("未找到消息");
          await copyText("content" in item ? item.content : item.kind === "tool" ? item.result ?? item.arguments : JSON.stringify(item));
          result = "已复制";
        } else if (operation.local === "pin") {
          result = { pinnedProjectIds: await togglePin(action.values.projectId!) };
        } else if (operation.local === "shell") {
          let terminal: { terminalId: string; running: boolean } | undefined;
          await suspendTerminal(async () => {
            terminal = await ptyHubRef.current!.attach(action.values.sessionId!, action.values.terminalId!);
          });
          result = terminal?.running ? `已返回 CLI；PTY ${terminal.terminalId} 仍在运行，再次打开可继续。` : "已返回 CLI；终端标签已关闭。";
        } else {
          result = await client.workbenchRequest(request.path, operation.method, request.body);
        }
        if (operation.id === "projects") result = { pinnedProjectIds: await readPins(), projects: result };
        if (operation.opensSession && result && typeof result === "object") {
          const target = result as { id?: string; sessionId?: string };
          if (target.id || target.sessionId) { await loadSession((target.id ?? target.sessionId)!); setModel((value) => ({ ...value, workbenchOpen: false })); }
        }
        if (operation.id === "session-delete") {
          timelineCacheRef.current.delete(action.values.sessionId!);
          if (activeSessionRef.current === action.values.sessionId) await handleAction({ type: "new_session" });
        }
        if (operation.method !== "GET" && !operation.local) {
          // 写操作已经成功；附属列表刷新失败不得伪装成提交失败，诱导用户重复执行。
          const [sessions, teams, automations, models] = await Promise.allSettled([client.listSessions(), client.listTeams(), client.listScheduledTasks(), client.models()]);
          const failures = [sessions, teams, automations, models].filter((item) => item.status === "rejected");
          setModel((value) => ({
            ...value,
            ...(sessions.status === "fulfilled" ? { sessions: sessions.value, environment: sessions.value.find((item) => item.id === activeSessionRef.current)?.environment ?? null } : {}),
            ...(teams.status === "fulfilled" ? { teams: teams.value } : {}),
            ...(automations.status === "fulfilled" ? { automations: automations.value } : {}),
            ...(models.status === "fulfilled" ? { runtime: { ...value.runtime, modelCatalog: choicesFromModels(models.value), models: configRef.current.agentKind === "k_agent" ? choicesFromModels(models.value) : value.runtime.models } } : {}),
            ...(failures.length ? { notice: `操作成功；${failures.length} 项列表刷新失败，请手动刷新` } : {}),
          }));
        }
        if (["team-detail", "team-events", "auto-detail", "auto-runs", "auto-session", "integration"].includes(operation.id)) workbenchWatchRef.current = { operationId: operation.id, values: action.values };
        if (operation.id === "save-mcp" || operation.id === "install-mcp") await refreshMcp();
        if (["save-skills", "install-skills", "import-skill"].includes(operation.id)) {
          const payload = await client.getSkillsConfig();
          setModel((value) => ({ ...value, runtime: applySkillsToRuntime(value.runtime, payload.skills) }));
        }
        setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: false, result: result ?? "操作成功" } }));
      } catch (error) {
        setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: false, error: errorMessage(error) } }));
      }
      return;
    }
    if (action.type === "quit") {
      exit();
      return;
    }
    if (action.type === "retry_connection") {
      try {
        const health = await client.health();
        setModel((value) => ({ ...value, health, connected: health.ok, error: undefined }));
      } catch (error) { setModel((value) => ({ ...value, connected: false, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "select_session") {
      await loadSession(action.sessionId);
      return;
    }
    if (action.type === "enter_project") {
      setModel((value) => ({ ...value, activeProjectId: action.projectId, surface: "projects", error: undefined }));
      return;
    }
    if (action.type === "clear_project") {
      setModel((value) => ({ ...value, activeProjectId: undefined, surface: "projects" }));
      return;
    }
    if (action.type === "pin_project") {
      try {
        const pinnedProjectIds = await togglePin(action.projectId);
        setModel((value) => ({ ...value, pinnedProjectIds }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "browse_directory") {
      try {
        const directory = await client.browseDirectory(action.path);
        setModel((value) => ({ ...value, directory, error: undefined }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "make_directory") {
      try {
        await client.makeDirectory(action.parentPath, action.name);
        const directory = await client.browseDirectory(action.parentPath);
        setModel((value) => ({ ...value, directory, notice: `已创建 ${action.name}` }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "create_project") {
      try {
        const project = await client.createProject(action.rootPath);
        setModel((value) => ({ ...value, projects: [project, ...value.projects.filter((item) => item.id !== project.id)], activeProjectId: project.id, directory: undefined, surface: "projects", notice: `已进入「${project.name}」` }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "load_branches") {
      try {
        const repository = await client.projectRepository(action.projectId);
        setModel((value) => ({ ...value, projectBranches: { projectId: action.projectId, branches: repository.branches }, error: undefined }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "set_target_branch") {
      try {
        const project = await client.updateProject(action.projectId, action.name, action.targetBranch);
        setModel((value) => ({ ...value, projects: value.projects.map((item) => item.id === project.id ? project : item), notice: action.targetBranch ? `目标分支 ${action.targetBranch}` : "目标分支改为跟随当前分支" }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "start_project_session") {
      try {
        const session = await client.createProjectSession(action.projectId, action.environment, action.startingState);
        setModel((value) => ({ ...value, sessions: [session, ...value.sessions.filter((item) => item.id !== session.id)], activeProjectId: action.projectId }));
        await loadSession(session.id);
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "fork_session") {
      if (model.timeline.runStatus === "running" && model.activeSessionId === action.sessionId) {
        setModel((value) => ({ ...value, notice: "当前 Run 未结束" }));
        return;
      }
      try {
        const session = await client.forkSession(action.sessionId, { environment: action.environment, ...(action.throughMessageId ? { throughMessageId: action.throughMessageId } : {}) });
        setModel((value) => ({ ...value, sessions: [session, ...value.sessions.filter((item) => item.id !== session.id)], notice: action.environment === "worktree" ? "已在隔离目录创建分支" : "已在同一目录创建分支" }));
        await loadSession(session.id);
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "bind_session") {
      try {
        const session = await client.bindSessionToProject(action.sessionId, action.projectId);
        setModel((value) => ({ ...value, sessions: value.sessions.map((item) => item.id === session.id ? session : item), notice: "会话已移入当前项目" }));
        if (activeSessionRef.current === session.id) await loadSession(session.id);
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "delete_session") {
      try {
        await client.deleteSession(action.sessionId);
        timelineCacheRef.current.delete(action.sessionId);
        const sessions = await client.listSessions();
        setModel((value) => ({ ...value, sessions, notice: "会话已删除" }));
        if (activeSessionRef.current === action.sessionId) {
          activeSessionRef.current = undefined;
          setModel((value) => ({ ...value, activeSessionId: undefined, activeSessionTitle: undefined, environment: undefined, timeline: emptyTimeline(), surface: value.activeProjectId ? "projects" : "chat" }));
        }
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "handoff_session") {
      const sessionId = activeSessionRef.current;
      if (!sessionId) return void setModel((value) => ({ ...value, notice: "请先打开 Session" }));
      try {
        const session = await client.handoffSession(sessionId, action.target);
        setModel((value) => ({ ...value, sessions: value.sessions.map((item) => item.id === session.id ? session : item), environment: session.environment ?? null, notice: action.target === "worktree" ? "已切到 Worktree" : "已切到本地目录" }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "compact_session") {
      const sessionId = activeSessionRef.current;
      if (!sessionId) return void setModel((value) => ({ ...value, notice: "请先打开 Session" }));
      try {
        const status = await client.compactSession(sessionId, action.instructions, action.reset === true);
        const saved = typeof status.savedTokens === "number" ? `，约节省 ${status.savedTokens} token` : "";
        setModel((value) => ({ ...value, notice: action.reset ? "已重置自动压缩" : `上下文已压缩${saved}` }));
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "new_session") {
      activeSessionRef.current = undefined;
      setModel((value) => {
        const project = value.projects.find((item) => item.id === value.activeProjectId);
        return { ...value, surface: "chat", activeSessionId: undefined, activeSessionTitle: undefined, environment: undefined, workspace: undefined, workspaceFile: undefined, timeline: emptyTimeline(), notice: project ? `新对话将在「${project.name}」中创建` : "新 Session 将在首次发送时创建" };
      });
      return;
    }
    if (action.type === "open_surface") {
      setModel((value) => ({ ...value, surface: action.surface }));
      return;
    }
    if (action.type === "select_activity") {
      setModel((value) => ({ ...value, selectedActivityId: action.activityId }));
      return;
    }
    if (action.type === "submit_prompt") {
      if (!configRef.current.modelId && configRef.current.agentKind === "k_agent") {
        setModel((value) => ({ ...value, error: "没有可用模型，请按 Ctrl+G，在配置中启用模型" }));
        return;
      }
      let sessionForRun = activeSessionRef.current;
      if (!sessionForRun && activeProjectRef.current) {
        try {
          const created = await client.createProjectSession(activeProjectRef.current, "auto", "default");
          sessionForRun = created.id;
          activeSessionRef.current = created.id;
          setModel((value) => ({ ...value, sessions: [created, ...value.sessions.filter((item) => item.id !== created.id)] }));
        } catch (error) {
          setModel((value) => ({ ...value, error: errorMessage(error) }));
          return;
        }
      }
      const run = createRunInput(action.text, configRef.current, sessionForRun);
      if (model.attachments?.length) run.input.forwardedProps.attachments = model.attachments;
      setModel((value) => ({ ...value, attachments: [] }));
      activeSessionRef.current = run.sessionId;
      const previous = timelineCacheRef.current.get(run.sessionId) ?? emptyTimeline();
      const timeline = appendUserPrompt(previous, run.userMessageId, action.text, run.runId, model.attachments);
      timelineCacheRef.current.set(run.sessionId, { ...timeline, runStatus: "running" });
      setModel((value) => ({ ...value, surface: "chat", activeSessionId: run.sessionId, activeSessionTitle: action.text.slice(0, 40), timeline: { ...timeline, runStatus: "running" }, error: undefined }));
      await startRun(run.sessionId, run.input);
      return;
    }
    if (action.type === "stop_run" || action.type === "cancel_run") {
      const sessionId = activeSessionRef.current;
      const activeRun = sessionId ? runsRef.current.get(sessionId) : undefined;
      const runId = activeRun?.runId ?? model.timeline.runId;
      if (!sessionId || !runId || model.timeline.runStatus !== "running") {
        setModel((value) => ({ ...value, notice: "当前 Session 没有运行中的任务" }));
        return;
      }
      try {
        if (action.type === "stop_run") await client.stopRun(sessionId, runId);
        else await client.cancelRun(sessionId, runId);
        activeRun?.controller.abort();
        if (action.type === "stop_run") {
          const current = timelineCacheRef.current.get(sessionId) ?? emptyTimeline();
          const stopped = projectEvent(current, { type: "RUN_FINISHED", threadId: sessionId, runId, result: { status: "stopped" } });
          timelineCacheRef.current.set(sessionId, stopped);
          setModel((value) => ({ ...value, timeline: stopped, notice: "运行已停止，已生成内容保留" }));
        } else {
          await loadSession(sessionId);
          setModel((value) => ({ ...value, notice: "已请求服务端取消并按其规则回滚" }));
        }
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "open_workspace") {
      const sessionId = activeSessionRef.current;
      if (!sessionId) return void setModel((value) => ({ ...value, notice: "请先打开 Session" }));
      try {
        if (action.path) {
          const workspaceFile = await client.workspaceFile(sessionId, action.path);
          setModel((value) => ({ ...value, workspaceFile, notice: `Workspace: ${action.path}` }));
        } else {
          const workspace = await client.workspace(sessionId);
          setModel((value) => ({ ...value, workspace, workspaceFile: undefined, notice: `Workspace 文件 ${workspace.files.length}` }));
        }
      } catch (error) { setModel((value) => ({ ...value, error: errorMessage(error) })); }
      return;
    }
    if (action.type === "answer_interrupt") {
      const sessionId = activeSessionRef.current;
      const approval = pendingApproval(model.timeline);
      if (!sessionId || !approval || approval.id !== action.interruptId) return;
      const submitting = updateApprovalStatus(model.timeline, approval.id, "submitting");
      timelineCacheRef.current.set(sessionId, submitting);
      setModel((value) => ({ ...value, timeline: submitting, error: undefined }));
      const decision = action.payload.action === "answer"
        ? { action: "answer" as const, answers: action.payload.answers }
        : action.payload.action === "approve"
          ? { action: "approve" as const, scope: action.payload.scope }
          : { action: action.payload.action as "deny" | "cancel" };
      await startRun(sessionId, createResumeInput(approval, configRef.current, decision));
      // Access Layer 在 Resume 结束后才把最终审批快照写进 history，本次 SSE 不会再推回来。
      // 这里按提交的动作就地收口，否则卡片永远停在 submitting，下一次输入也会被它挡住。
      settleApproval(sessionId, approval.id, decision.action);
      return;
    }
    if (action.type === "toggle_capability") {
      const items = action.kind === "mcp" ? model.runtime.mcpServers : model.runtime.skills;
      const key = action.kind === "mcp" ? "mcpServerIds" : "skillIds";
      const selected = configRef.current[key];
      const enabled = items.filter((item) => item.enabled).map((item) => item.id);
      const nextIds = action.selection === "all" ? enabled : action.selection === "none" ? [] : selected.includes(action.id) ? selected.filter((id) => id !== action.id) : enabled.includes(action.id) ? [...selected, action.id] : selected;
      applyCliRuntime({ ...configRef.current, [key]: nextIds }, "本轮能力选择已更新，下一轮生效");
      return;
    }
    if (action.type === "refresh_mcp") {
      await refreshMcp();
      return;
    }
    if (action.type === "reload_mcp") {
      await mutateMcp(async (access) => {
        await access.reloadMcp();
        return "MCP 已重新连接";
      });
      return;
    }
    if (action.type === "toggle_mcp") {
      await mutateMcp(async (access) => {
        const payload = await access.getMcpConfig();
        const currentlyOn = payload.servers.find((item) => item.id === action.serverId)?.enabled !== false;
        const next = setMcpEnabled(payload.servers, action.serverId, !currentlyOn);
        if (next.missing) return `未找到 MCP “${action.serverId}”`;
        if (next.changed.length === 0) return `MCP ${action.serverId} 状态未变化`;
        await access.saveMcpConfig(sanitizeMcpServersForSave(next.servers));
        return `MCP ${action.serverId} 已${currentlyOn ? "关闭" : "启用"}`;
      });
      return;
    }
    if (action.type === "refresh_skills") {
      try {
        const payload = await client.getSkillsConfig();
        setModel((value) => ({ ...value, runtime: applySkillsToRuntime(value.runtime, payload.skills) }));
      } catch (error) {
        setModel((value) => ({ ...value, error: errorMessage(error) }));
      }
      return;
    }
    if (action.type === "toggle_skill") {
      await mutateSkills(async (access) => {
        const payload = await access.getSkillsConfig();
        const currentlyOn = payload.skills.find((item) => item.id === action.skillId)?.enabled !== false;
        const next = setSkillEnabled(payload.skills, action.skillId, !currentlyOn);
        if (next.missing) return `未找到 Skill “${action.skillId}”`;
        if (next.changed.length === 0) return `Skill ${action.skillId} 状态未变化`;
        await access.saveSkillsConfig(sanitizeSkillsForSave(next.skills));
        return `Skill ${action.skillId} 已${currentlyOn ? "关闭" : "启用"}`;
      });
      return;
    }
    if (action.type === "set_model") {
      applyChoice(selectModel(configRef.current, model.runtime.models, action.modelId), applyCliRuntime, setModel);
      return;
    }
    if (action.type === "set_agent") {
      applyChoice(selectAgent(configRef.current, model.runtime.agents, action.agentKind), applyCliRuntime, setModel);
      return;
    }
    if (action.type === "set_permission") {
      applyChoice(selectPermission(configRef.current, action.permissionMode), applyCliRuntime, setModel);
      return;
    }
    if (action.type === "slash_command") await handleSlash(action.command, action.arguments);
  }, [applyCliRuntime, client, exit, suspendTerminal, loadSession, model.activeSessionId, model.runtime.agents, model.runtime.models, model.runtime.mcpServers, model.runtime.skills, model.timeline, model.attachments, mutateMcp, mutateSkills, refreshMcp, settleApproval, startRun]);

  const handleSlash = useCallback(async (command: string, args: string): Promise<void> => {
    if (command === "workbench") return void handleAction({ type: "open_workbench" });
    if (command === "projects") return void handleAction({ type: "open_surface", surface: "projects" });
    if (command === "new") return void handleAction({ type: "new_session" });
    if (command === "fork") {
      const sessionId = activeSessionRef.current;
      if (!sessionId) return void setModel((value) => ({ ...value, notice: "请先打开 Session" }));
      const parts = args.split(/\s+/).filter(Boolean);
      const environment = parts.includes("worktree") ? "worktree" as const : "same-directory" as const;
      const explicit = parts.find((part) => part !== "worktree" && part !== "here" && part !== "same-directory");
      const throughMessageId = parts.includes("here")
        ? [...model.timeline.items].reverse().find((item) => item.kind === "text")?.id
        : explicit;
      return void handleAction({ type: "fork_session", sessionId, environment, ...(throughMessageId ? { throughMessageId } : {}) });
    }
    if (command === "compact") {
      const reset = args.trim() === "reset";
      return void handleAction({ type: "compact_session", instructions: reset ? "" : args.trim(), reset });
    }
    if (command === "handoff") {
      const requested = args.trim();
      const target = requested === "local" || requested === "worktree"
        ? requested
        : model.environment?.kind === "worktree" ? "local" as const : "worktree" as const;
      return void handleAction({ type: "handoff_session", target });
    }
    if (command === "home" || command === "chat") return void handleAction({ type: "open_surface", surface: command === "home" ? "home" : "chat" });
    if (command === "team") return void handleAction({ type: "open_surface", surface: "team" });
    if (command === "auto" || command === "automation") return void handleAction({ type: "open_surface", surface: "automation" });
    if (command === "doctor") return void handleAction({ type: "open_surface", surface: "doctor" });
    if (command === "sessions") return void setModel((value) => ({ ...value, notice: "按 Ctrl+P 打开 Session Switcher" }));
    if (command === "workspace") return void handleAction({ type: "open_workspace", ...(args ? { path: args } : {}) });
    if (command === "stop") return void handleAction({ type: "stop_run" });
    if (command === "cancel") return void handleAction({ type: "cancel_run" });
    if (command === "quit") return void handleAction({ type: "quit" });
    if (command === "mcp") {
      const parsed = parseMcpSlashArgs(args);
      if (parsed.kind === "reload") return void handleAction({ type: "reload_mcp" });
      if (parsed.kind === "enable" || parsed.kind === "disable") {
        await mutateMcp(async (clientRef) => {
          const payload = await clientRef.getMcpConfig();
          const next = setMcpEnabled(payload.servers, parsed.target, parsed.kind === "enable");
          if (next.missing) return `未找到 MCP “${parsed.target}”`;
          if (next.changed.length === 0) return `MCP 已是${parsed.kind === "enable" ? "启用" : "关闭"}状态`;
          await clientRef.saveMcpConfig(sanitizeMcpServersForSave(next.servers));
          return parsed.target === "all"
            ? `已${parsed.kind === "enable" ? "启用" : "关闭"} ${next.changed.length} 个 MCP`
            : `MCP ${parsed.target} 已${parsed.kind === "enable" ? "启用" : "关闭"}`;
        });
        return;
      }
      return void setModel((value) => ({
        ...value,
        notice: "用法：/mcp · /mcp enable [name|all] · /mcp disable [name|all] · /mcp reload",
      }));
    }
    if (command === "model") {
      const parsed = parseMenuOrSetArgs(args);
      if (parsed.kind === "set") {
        applyChoice(selectModel(configRef.current, model.runtime.models, parsed.target), applyCliRuntime, setModel);
        return;
      }
      return void setModel((value) => ({ ...value, notice: "用法：/model · /model <id>" }));
    }
    if (command === "agent") {
      const parsed = parseMenuOrSetArgs(args);
      if (parsed.kind === "set") {
        applyChoice(selectAgent(configRef.current, model.runtime.agents, parsed.target), applyCliRuntime, setModel);
        return;
      }
      return void setModel((value) => ({ ...value, notice: "用法：/agent · /agent <kind>" }));
    }
    if (command === "permissions") {
      const parsed = parseMenuOrSetArgs(args);
      if (parsed.kind === "set") {
        applyChoice(selectPermission(configRef.current, parsed.target), applyCliRuntime, setModel);
        return;
      }
      return void setModel((value) => ({ ...value, notice: "用法：/permissions · /permissions default|full_access" }));
    }
    if (command === "skill") {
      const parsed = parseSkillSlashArgs(args);
      if (parsed.kind === "enable" || parsed.kind === "disable") {
        await mutateSkills(async (clientRef) => {
          const payload = await clientRef.getSkillsConfig();
          const next = setSkillEnabled(payload.skills, parsed.target, parsed.kind === "enable");
          if (next.missing) return `未找到 Skill “${parsed.target}”`;
          if (next.changed.length === 0) return `Skill 已是${parsed.kind === "enable" ? "启用" : "关闭"}状态`;
          await clientRef.saveSkillsConfig(sanitizeSkillsForSave(next.skills));
          return parsed.target === "all"
            ? `已${parsed.kind === "enable" ? "启用" : "关闭"} ${next.changed.length} 个 Skill`
            : `Skill ${parsed.target} 已${parsed.kind === "enable" ? "启用" : "关闭"}`;
        });
        return;
      }
      return void setModel((value) => ({
        ...value,
        notice: "用法：/skill · /skill enable [name|all] · /skill disable [name|all]",
      }));
    }
    if (["trace", "help"].includes(command)) {
      setModel((value) => ({ ...value, notice: slashSummary(command, value) }));
      return;
    }
    setModel((value) => ({ ...value, notice: `未知命令 /${command}；输入 /help 查看可用命令` }));
  }, [applyCliRuntime, handleAction, model.environment?.kind, model.runtime.agents, model.runtime.models, model.timeline.items, mutateMcp, mutateSkills]);

  useEffect(() => {
    if (model.surface !== "skills" && model.surface !== "connectors") return;
    const kind = model.surface === "connectors" ? "mcp" : "skill";
    let cancelled = false;
    setModel((value) => ({ ...value, marketplace: { kind, loading: true, items: value.marketplace?.kind === kind ? value.marketplace.items : [] } }));
    const path = kind === "mcp" ? "/api/marketplace/mcp?page=1&pageSize=24" : "/api/marketplace/skills?page=1&pageSize=24";
    void client.workbenchRequest(path).then((result) => {
      if (cancelled) return;
      const record = result as { items?: Array<Record<string, unknown>>; warnings?: string[]; sourceStatus?: string };
      const items = (record.items ?? []).flatMap((item) => {
        const sourceId = String(item.sourceId ?? "");
        const title = String(item.title ?? sourceId);
        if (!sourceId && !title) return [];
        return [{ sourceId: sourceId || title, title, summary: String(item.summary ?? ""), installed: item.installed === true }];
      });
      const notice = record.sourceStatus === "unavailable" ? (record.warnings?.[0] || "市场暂时不可用，本机会话不受影响") : undefined;
      setModel((value) => ({ ...value, marketplace: { kind, loading: false, items, ...(notice ? { notice } : {}) } }));
    }).catch((error) => {
      if (!cancelled) setModel((value) => ({ ...value, marketplace: { kind, loading: false, items: [], notice: errorMessage(error) } }));
    });
    return () => { cancelled = true; };
  }, [client, model.surface]);

  useEffect(() => {
    let disposed = false;
    let pending = false;
    const timer = setInterval(() => {
      if (pending) return;
      pending = true;
      void (async () => {
        try {
          const watch = workbenchWatchRef.current;
          if (model.workbenchOpen && watch) {
            const operation = WORKBENCH_OPERATIONS.find((item) => item.id === watch.operationId)!;
            const request = operationRequest(operation, watch.values);
            const result = await client.workbenchRequest(request.path);
            if (!disposed && workbenchWatchRef.current === watch) setModel((value) => ({ ...value, workbench: { operationId: operation.id, busy: false, result } }));
          }
          const sessionId = activeSessionRef.current;
          if (sessionId && !runsRef.current.has(sessionId)) {
            const session = await client.getSession(sessionId);
            if (!disposed && activeSessionRef.current === sessionId && !runsRef.current.has(sessionId)) {
              const timeline = timelineFromSession(session);
              timelineCacheRef.current.set(sessionId, timeline);
              setModel((value) => ({ ...value, timeline, environment: session.environment ?? null }));
            }
          }
        } catch (error) {
          if (!disposed) setModel((value) => ({ ...value, notice: `状态刷新失败：${errorMessage(error)}` }));
        } finally { pending = false; }
      })();
    }, 2500);
    return () => { disposed = true; clearInterval(timer); };
  }, [client, model.workbenchOpen]);

  return <TerminalPage model={model} onAction={(action) => { void handleAction(action); }} />;
}

function doctorSummary(health: TerminalPageViewModel["health"], agents: string[], modelCount: number): string[] {
  if (!health) return [];
  return [
    `${health.ok ? "✓" : "×"} Access Layer`,
    `${health.agentBackendOk ? "✓" : "×"} Agent Backend`,
    `Tools local=${health.localToolCount} mcp=${health.mcpToolCount}`,
    `Agents ${agents.join(", ") || "none"}`,
    `Models ${modelCount} enabled`,
  ];
}

function slashSummary(command: string, model: TerminalPageViewModel): string {
  if (command === "agent") return `Agent ${model.runtime.agentKind}`;
  if (command === "model") return `Model ${model.runtime.modelId || "未选择"}`;
  if (command === "mcp") return `MCP ${model.runtime.mcpCount}`;
  if (command === "skill") return `Skills ${model.runtime.skillCount}`;
  if (command === "permissions") return `Permission ${model.runtime.permissionMode}`;
  if (command === "trace") return `Timeline items ${model.timeline.items.length}`;
  return "/home /chat /team /auto /doctor /new /sessions /agent /model /mcp /skill /permissions /trace /workspace /stop /cancel /help /quit";
}

async function refreshMcpSnapshot(
  client: AccessLayerClient,
): Promise<{ servers: Awaited<ReturnType<AccessLayerClient["getMcpConfig"]>>["servers"]; tools: ReturnType<typeof toolsFromCapabilities> }> {
  const [config, capabilities] = await Promise.all([
    client.getMcpConfig(),
    client.mcpCapabilities().catch(() => ({ tools: [] })),
  ]);
  return { servers: config.servers, tools: toolsFromCapabilities(capabilities) };
}

function applyChoice(
  result: { config: CliRuntimeConfig; notice: string } | { error: string },
  apply: (config: CliRuntimeConfig, notice: string) => void,
  setView: React.Dispatch<React.SetStateAction<TerminalPageViewModel>>,
): void {
  if ("error" in result) {
    setView((value) => ({ ...value, error: result.error }));
    return;
  }
  apply(result.config, result.notice);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const RESUME_RESULT_STATUS: Record<"approve" | "deny" | "cancel" | "answer", ApprovalActivity["status"]> = {
  approve: "approved",
  deny: "denied",
  cancel: "cancelled",
  answer: "answered",
};

function updateApprovalStatus(
  timeline: TimelineState,
  approvalId: string,
  status: ApprovalActivity["status"],
): TimelineState {
  return {
    ...timeline,
    items: timeline.items.map((item) => item.kind === "approval" && item.id === approvalId
      ? { ...item, approval: { ...item.approval, status } }
      : item),
  };
}
