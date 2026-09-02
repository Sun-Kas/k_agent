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
  const { exit } = useApp();
  const [config, setConfig] = useState(initialConfig);
  const configRef = useRef(config);
  configRef.current = config;
  const [model, setModel] = useState<TerminalPageViewModel>(() => ({ ...emptyViewModel(initialConfig), surface: startAt }));
  const activeSessionRef = useRef<string | undefined>(initialSessionId);
  const timelineCacheRef = useRef(new Map<string, TimelineState>());
  const runsRef = useRef(new Map<string, ActiveRun>());

  const loadSession = useCallback(async (sessionId: string): Promise<void> => {
    activeSessionRef.current = sessionId;
    const cached = timelineCacheRef.current.get(sessionId);
    if (cached) {
      setModel((value) => ({ ...value, surface: "chat", activeSessionId: sessionId, timeline: cached, error: undefined }));
      return;
    }
    setModel((value) => ({ ...value, loading: true, surface: "chat", activeSessionId: sessionId }));
    try {
      const session = await client.getSession(sessionId);
      const timeline = timelineFromSession(session);
      timelineCacheRef.current.set(sessionId, timeline);
      if (activeSessionRef.current === sessionId) {
        setModel((value) => ({ ...value, loading: false, timeline, error: undefined }));
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
      setModel((value) => ({
        ...value,
        mcpBusy: false,
        ...(notice ? { notice } : {}),
        runtime: applyMcpServersToRuntime(value.runtime, snapshot.servers, snapshot.tools),
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
      setModel((value) => ({
        ...value,
        mcpBusy: false,
        notice,
        runtime: applySkillsToRuntime(value.runtime, payload.skills),
      }));
    } catch (error) {
      setModel((value) => ({ ...value, mcpBusy: false, error: errorMessage(error) }));
    }
  }, [client]);

  const applyCliRuntime = useCallback((nextConfig: CliRuntimeConfig, notice: string): void => {
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
      setModel((value) => ({
        ...value,
        mcpBusy: false,
        notice,
        runtime: applyMcpServersToRuntime(value.runtime, snapshot.servers, snapshot.tools),
      }));
    } catch (error) {
      setModel((value) => ({ ...value, mcpBusy: false, error: errorMessage(error) }));
    }
  }, [client]);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const [health, sessions, catalog, agents, models, teams, automations] = await Promise.all([
          client.health(),
          client.listSessions(),
          client.catalog(),
          client.agents(),
          client.models(),
          client.listTeams().catch(() => []),
          client.listScheduledTasks().catch(() => []),
        ]);
        if (!active) return;
        const selectedModel = config.modelId ?? models.find((item) => item.enabled)?.id;
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
          teams,
          automations,
          runtime: {
            ...value.runtime,
            modelId: selectedModel ?? "",
            models: modelChoices,
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
            mcpCount: nextConfig.mcpServerIds.length || catalog.mcpServers.filter((item) => item.enabled).length,
            skillCount: nextConfig.skillIds.length || catalog.skills.filter((item) => item.enabled).length,
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
        const failed = input.resume?.length ? updateApprovalStatus(current, input.resume[0]!.interruptId, "resume_failed") : current;
        timelineCacheRef.current.set(sessionId, failed);
        setModel((value) => ({ ...value, timeline: failed, error: `事件流中断：${errorMessage(error)}` }));
      }
    } finally {
      runsRef.current.delete(sessionId);
      await refreshSessions();
    }
  }, [client, refreshSessions]);

  const handleAction = useCallback(async (action: TerminalPageAction): Promise<void> => {
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
    if (action.type === "new_session") {
      activeSessionRef.current = undefined;
      setModel((value) => ({ ...value, surface: "chat", activeSessionId: undefined, activeSessionTitle: undefined, timeline: emptyTimeline(), notice: "新 Session 将在首次发送时创建" }));
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
        setModel((value) => ({ ...value, error: "没有可用模型，请先在 Web 配置中启用模型" }));
        return;
      }
      const run = createRunInput(action.text, configRef.current, activeSessionRef.current);
      activeSessionRef.current = run.sessionId;
      const previous = timelineCacheRef.current.get(run.sessionId) ?? emptyTimeline();
      const timeline = appendUserPrompt(previous, run.userMessageId, action.text, run.runId);
      timelineCacheRef.current.set(run.sessionId, { ...timeline, runStatus: "running" });
      setModel((value) => ({ ...value, surface: "chat", activeSessionId: run.sessionId, activeSessionTitle: action.text.slice(0, 40), timeline: { ...timeline, runStatus: "running" }, error: undefined }));
      await startRun(run.sessionId, run.input);
      return;
    }
    if (action.type === "stop_run" || action.type === "cancel_run") {
      const sessionId = activeSessionRef.current;
      const activeRun = sessionId ? runsRef.current.get(sessionId) : undefined;
      if (!sessionId || !activeRun) {
        setModel((value) => ({ ...value, notice: "当前 Session 没有运行中的任务" }));
        return;
      }
      try {
        if (action.type === "stop_run") await client.stopRun(sessionId, activeRun.runId);
        else await client.cancelRun(sessionId, activeRun.runId);
        activeRun.controller.abort();
        if (action.type === "stop_run") {
          const current = timelineCacheRef.current.get(sessionId) ?? emptyTimeline();
          const stopped = projectEvent(current, { type: "RUN_FINISHED", threadId: sessionId, runId: activeRun.runId, result: { status: "stopped" } });
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
  }, [applyCliRuntime, client, exit, loadSession, model.runtime.agents, model.runtime.models, model.timeline, mutateMcp, mutateSkills, refreshMcp, startRun]);

  const handleSlash = useCallback(async (command: string, args: string): Promise<void> => {
    if (command === "new") return void handleAction({ type: "new_session" });
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
  }, [applyCliRuntime, handleAction, model.runtime.agents, model.runtime.models, mutateMcp, mutateSkills]);

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
