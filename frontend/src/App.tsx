/**
 * 单会话聊天主界面：组装 AG-UI run 请求、消费 SSE 事件、维护会话/run 状态机。
 * 关键路径：submitConversation → streamAgentRun → applyAgUiEvent → messages/tools/thinking UI。
 */
import {
  Component,
  FormEvent,
  KeyboardEvent,
  PointerEvent as ReactPointerEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState
} from "react";
import {
  cancelSessionRun,
  getAgentsCatalog,
  getHealth,
  getModelsConfig,
  getRuntimeCatalog,
  getSession,
  getSessionWorkspace,
  getSessionWorkspaceFile,
  listSessions,
  resolveApproval,
  streamAgentRun
} from "./api/agui";
import { MarkdownContent } from "./components/MarkdownContent";
import { groupDisplayMessages, InlineToolActivity, toolResultFailed } from "./components/ConversationTranscript";
import { ConfigCenter } from "./components/ConfigCenter";
import { ContentStage, type ContentStageItem } from "./components/ContentStage";
import { TeamWorkbench } from "./components/TeamWorkbench";
import { ScheduledTasksView } from "./components/ScheduledTasksView";
import { DesktopPet } from "./components/DesktopPet";
import { appConfig } from "./config";
import { mergeHistoricalMessages } from "./history";
import { createClientId } from "./id";
import { readVoiceConfig, type VoiceConfig } from "./voice-config";
import { shouldInterruptForTranscript } from "./voice-interruption";
import type {
  AgUiEvent,
  AgUiRunInput,
  AgentKind,
  ApprovalActivity,
  ChatMessage,
  CliSessionMode,
  DetectedAgent,
  HealthState,
  ModelProfile,
  ReasoningEffort,
  RuntimeOption,
  SessionSummary,
  TextActivity,
  ThinkingActivity,
  ToolActivity
} from "./types";

const AGENT_KIND_STORAGE_KEY = "k-agent-agent-kind";
const CLI_SESSION_MODE_STORAGE_KEY = "k-agent-cli-session-mode";
const MODEL_BY_AGENT_STORAGE_KEY = "k-agent-model-by-agent";
const ACTIVE_VIEW_STORAGE_KEY = "k-agent-active-view";

function readModelByAgent(): Record<string, string> {
  try {
    const raw = localStorage.getItem(MODEL_BY_AGENT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[1] === "string")
    );
  } catch {
    return {};
  }
}
function writeModelForAgent(kind: AgentKind, modelId: string) {
  const next = { ...readModelByAgent(), [kind]: modelId };
  localStorage.setItem(MODEL_BY_AGENT_STORAGE_KEY, JSON.stringify(next));
}

const suggestions = [
  "帮我拆解这个需求，并给出执行计划",
  "总结当前项目结构和关键模块",
  "检查可用工具，并说明能完成什么"
];

const colorThemes = [
  { id: "snow", name: "白色", colors: ["#ffffff", "#ffffff", "#111111"] },
  { id: "ink", name: "黑色", colors: ["#000000", "#141414", "#f5f5f5"] },
  { id: "blueprint", name: "蓝图", colors: ["#e8eef5", "#fafcff", "#2b59ff"] },
  { id: "forest", name: "石墨", colors: ["#10141c", "#1a2230", "#5b8cff"] },
  { id: "ocean", name: "港湾", colors: ["#07131c", "#122433", "#3ec4e0"] },
  { id: "sand", name: "锻铜", colors: ["#17120f", "#271e17", "#e08a3c"] },
  { id: "dusk", name: "余烬", colors: ["#141210", "#25201c", "#ff6b3d"] }
] as const;
type ColorTheme = typeof colorThemes[number]["id"];
type ThinkingBlock = {
  id: string;
  turnId: string;
  steps: ThinkingActivity[];
  closed: boolean;
  sequence: number;
};

type InlineActivity =
  | { type: "thinking"; sequence: number; block: ThinkingBlock }
  | { type: "tool"; sequence: number; tool: ToolActivity }
  | { type: "approval"; sequence: number; approval: ApprovalActivity }
  | { type: "text"; sequence: number; text: TextActivity };

type CopyFeedback = {
  messageId: string;
  status: "success" | "error";
};

type VoicePlaybackState = {
  supported: boolean;
  autoEnabled: boolean;
  status: "idle" | "waiting" | "speaking" | "error" | "unsupported";
  message: string;
  messageId?: string;
};

type VoiceConversationState = {
  supported: boolean;
  active: boolean;
  inputMuted: boolean;
  phase: "inactive" | "listening" | "processing" | "speaking" | "error";
  message: string;
};

/** 进行中的 run：按 session 缓冲事件，切回会话时可重放；AbortController 用于停止。 */
type ActiveConversationRun = {
  controller: AbortController;
  events: AgUiEvent[];
  initialMessages: ChatMessage[];
  pendingAssistantId: string;
  runId: string;
  userMessageId: string;
};

const createMessage = (role: ChatMessage["role"], content: string): ChatMessage => ({
  id: createClientId(),
  role,
  content,
  createdAt: new Date().toISOString()
});

function normalizeMessages(messages: unknown): ChatMessage[] {
  if (!Array.isArray(messages)) return [];

  return messages.map((message, index) => {
    const record = message && typeof message === "object" ? message as Record<string, unknown> : {};
    const role = isChatRole(record.role) ? record.role : "assistant";
    const rawCreatedAt = record.createdAt ?? record.created_at;
    const createdAt = typeof rawCreatedAt === "string" && !Number.isNaN(new Date(rawCreatedAt).getTime())
      ? rawCreatedAt
      : new Date().toISOString();

    return {
      id: typeof record.id === "string" && record.id ? record.id : `message-${index}-${createdAt}`,
      role,
      content: typeof record.content === "string"
        ? record.content
        : record.content == null
          ? ""
          : safeStringify(record.content),
      createdAt,
      meta: record.meta && typeof record.meta === "object" ? record.meta as ChatMessage["meta"] : undefined
    };
  });
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function isChatRole(value: unknown): value is ChatMessage["role"] {
  return value === "user" || value === "assistant" || value === "system" || value === "tool";
}

function normalizeSessionSummaries(sessions: unknown): SessionSummary[] {
  if (!Array.isArray(sessions)) return [];

  return sessions.flatMap((session) => {
    if (!session || typeof session !== "object") return [];
    const record = session as Record<string, unknown>;
    if (typeof record.id !== "string" || !record.id) return [];
    const rawUpdatedAt = record.updatedAt ?? record.updated_at;
    const updatedAt = typeof rawUpdatedAt === "string" && !Number.isNaN(new Date(rawUpdatedAt).getTime())
      ? rawUpdatedAt
      : new Date().toISOString();

    return [{
      id: record.id,
      title: typeof record.title === "string" && record.title ? record.title : "新会话",
      updatedAt,
      messageCount: typeof record.messageCount === "number" ? record.messageCount : 0
    }];
  });
}

function normalizeStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function normalizeThinking(value: unknown): ThinkingActivity[] {
  if (!Array.isArray(value)) return [];

  return value.flatMap((step, index) => {
    if (!step || typeof step !== "object") return [];
    const record = step as Record<string, unknown>;
    if (record.phase === "tool") return [];
    // 旧版本会在正文结束后人为追加该步骤，既不是模型 thinking，
    // 也会制造一个多余的连续折叠组；加载历史时将它迁移掉。
    if (
      record.phase === "synthesis"
      && record.title === "整理最终回答"
      && record.detail === "已综合上下文与工具结果，完成回答。"
    ) {
      return [];
    }
    const phase = isThinkingPhase(record.phase) ? record.phase : "analysis";
    const status = isThinkingStatus(record.status) ? record.status : "complete";
    const rawCreatedAt = record.createdAt ?? record.created_at;
    const createdAt = typeof rawCreatedAt === "string" && !Number.isNaN(new Date(rawCreatedAt).getTime())
      ? rawCreatedAt
      : new Date().toISOString();

    return [{
      id: typeof record.id === "string" && record.id ? record.id : `thinking-${index}-${createdAt}`,
      phase,
      title: typeof record.title === "string" ? record.title : "思考步骤",
      detail: typeof record.detail === "string" ? record.detail : "",
      status,
      iteration: typeof record.iteration === "number" ? record.iteration : index + 1,
      createdAt
    }];
  });
}

function normalizeThinkingStep(value: unknown): ThinkingActivity | null {
  return normalizeThinking([value])[0] ?? null;
}

function inlineTimeline(
  texts: TextActivity[],
  blocks: ThinkingBlock[],
  tools: ToolActivity[],
  approvals: ApprovalActivity[]
): InlineActivity[] {
  return [
    ...texts.map((text) => ({ type: "text" as const, sequence: text.sequence, text })),
    ...blocks.map((block) => ({ type: "thinking" as const, sequence: block.sequence, block })),
    ...tools.map((tool, index) => ({ type: "tool" as const, sequence: tool.sequence ?? index * 2 + 1, tool })),
    ...approvals.map((approval) => ({ type: "approval" as const, sequence: approval.sequence, approval }))
  ].sort((left, right) => left.sequence - right.sequence);
}

function visibleTextLength(value: string): number {
  return value.replace(/\s/g, "").length;
}

function isThinkingPhase(value: unknown): value is ThinkingActivity["phase"] {
  return value === "analysis" || value === "reasoning" || value === "tool" || value === "synthesis" || value === "complete";
}

function isThinkingStatus(value: unknown): value is ThinkingActivity["status"] {
  return value === "active" || value === "complete" || value === "error";
}

/** 会话壳：会话切换 / 提交 run / AG-UI 事件投影到聊天与活动时间线。 */
export function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [trace, setTrace] = useState<string[]>([]);
  const [tasks, setTasks] = useState<string[]>([]);
  const [thinking, setThinking] = useState<ThinkingActivity[]>([]);
  const [thinkingBlocks, setThinkingBlocks] = useState<ThinkingBlock[]>([]);
  const [tools, setTools] = useState<ToolActivity[]>([]);
  const [textBlocks, setTextBlocks] = useState<TextActivity[]>([]);
  const [approvals, setApprovals] = useState<ApprovalActivity[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [status, setStatus] = useState<string>(appConfig.status.idle);
  const [input, setInput] = useState("");
  // Voice recognition can outlive the render that started a model run. Keep
  // the current composer value outside that stale closure so a new transcript
  // never reuses the previous turn after submit cleared the textarea.
  const composerInputRef = useRef("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [runningSessionIds, setRunningSessionIds] = useState<Set<string>>(() => new Set());
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const saved = localStorage.getItem("k-agent-sidebar-collapsed");
    return saved === null ? true : saved === "true";
  });
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [teamStageOpen, setTeamStageOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(() => Number(localStorage.getItem("k-agent-sidebar-width")) || 252);
  const [inspectorWidth, setInspectorWidth] = useState(() => Number(localStorage.getItem("k-agent-inspector-width")) || 360);
  const [rightPanelMode, setRightPanelMode] = useState<"trace" | "workspace">(() => {
    const saved = localStorage.getItem("k-agent-right-panel-mode");
    return saved === "workspace" ? "workspace" : "trace";
  });
  const [workspaceItems, setWorkspaceItems] = useState<ContentStageItem[]>([]);
  const [selectedWorkspacePath, setSelectedWorkspacePath] = useState<string | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const workspaceCacheRef = useRef<Map<string, string>>(new Map());
  const selectedWorkspacePathRef = useRef<string | null>(null);
  const [view, setView] = useState<"chat" | "config" | "team" | "scheduled">(() => (
    localStorage.getItem(ACTIVE_VIEW_STORAGE_KEY) === "scheduled" ? "scheduled" : "chat"
  ));
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [agents, setAgents] = useState<DetectedAgent[]>([]);
  const [agentKind, setAgentKind] = useState<AgentKind>(() => localStorage.getItem(AGENT_KIND_STORAGE_KEY) || "k_agent");
  const [cliSessionMode, setCliSessionMode] = useState<CliSessionMode>(() => {
    const saved = localStorage.getItem(CLI_SESSION_MODE_STORAGE_KEY);
    return saved === "resume" ? "resume" : "ephemeral";
  });
  const [mcpServers, setMcpServers] = useState<RuntimeOption[]>([]);
  const [skills, setSkills] = useState<RuntimeOption[]>([]);
  const [modelId, setModelId] = useState("");
  const [selectedMcp, setSelectedMcp] = useState<string[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("none");
  const [attachments, setAttachments] = useState<Array<{ name: string; dataUrl: string; type: string }>>([]);
  const [listening, setListening] = useState(false);
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback | null>(null);
  const [voicePlayback, setVoicePlayback] = useState<VoicePlaybackState>(() => {
    const supported = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
    return {
      supported,
      autoEnabled: false,
      status: supported ? "idle" : "unsupported",
      message: supported ? "" : "当前浏览器不支持语音输出"
    };
  });
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfig>(readVoiceConfig);
  const [voiceConversation, setVoiceConversation] = useState<VoiceConversationState>(() => ({
    supported: getSpeechRecognitionConstructor() !== null,
    active: false,
    inputMuted: false,
    phase: "inactive",
    message: ""
  }));
  const [desktopPetEnabled, setDesktopPetEnabled] = useState(
    () => localStorage.getItem("k-agent-desktop-pet-enabled") !== "false"
  );
  const composerTextareaRef = useRef<HTMLTextAreaElement>(null);
  const isComposerComposingRef = useRef(false);
  const [colorTheme, setColorTheme] = useState<ColorTheme>(() => {
    const savedTheme = localStorage.getItem("k-agent-color-theme");
    return colorThemes.some((theme) => theme.id === savedTheme) ? savedTheme as ColorTheme : "snow";
  });
  const [fontSizeDelta, setFontSizeDelta] = useState<number>(() => {
    const saved = localStorage.getItem("k-agent-font-size-delta");
    return saved ? Math.min(6, Math.max(-2, Number(saved))) : 0;
  });
  const [fontSizePopoverOpen, setFontSizePopoverOpen] = useState(false);
  const streamEndRef = useRef<HTMLDivElement>(null);
  const messageStreamRef = useRef<HTMLElement>(null);
  const streamPinnedRef = useRef(true);
  // Runs belong to sessions rather than the mounted chat view. Keeping their
  // controllers and received events here lets navigation detach and reattach
  // the visible conversation without cancelling or corrupting another session.
  const activeRunsRef = useRef<Map<string, ActiveConversationRun>>(new Map());
  const activeSessionIdRef = useRef<string | null>(null);
  const sessionNavigationTokenRef = useRef(0);
  const speechRef = useRef<{ start: () => void; stop: () => void } | null>(null);
  const voiceConversationActiveRef = useRef(false);
  const voiceInputMutedRef = useRef(false);
  const voiceRecognitionStopReasonRef = useRef<"submit" | "mute" | "exit" | null>(null);
  const voiceAutoSubmitPromptRef = useRef<string | null>(null);
  const voiceRunFinishedRef = useRef(true);
  const voiceAutoEnabledRef = useRef(voicePlayback.autoEnabled);
  const voiceSessionIdRef = useRef<string | null>(null);
  const voiceMessageIdRef = useRef<string | null>(null);
  const voiceBufferRef = useRef("");
  const voiceQueueRef = useRef<string[]>([]);
  const voiceSpeakingRef = useRef(false);
  const voiceEchoReferenceTextRef = useRef("");
  const voiceBargeInActiveRef = useRef(false);
  const voiceStoppingRef = useRef(false);
  const voiceGenerationRef = useRef(0);
  const pendingAssistantIdRef = useRef<string | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const persistedMessageIdsRef = useRef<Set<string>>(new Set());
  const replayingHistoryRef = useRef(false);
  const activeTextActivityIdRef = useRef<string | null>(null);
  const activeTextMessageIdRef = useRef<string | null>(null);
  const activeThinkingBlockIdRef = useRef<string | null>(null);
  const activeThinkingStepIdRef = useRef<string | null>(null);
  const activeThinkingTitleRef = useRef("思考过程");
  const assistantVisibleTextLengthRef = useRef(0);
  const activitySequenceRef = useRef(0);
  const copyFeedbackTimerRef = useRef<number | null>(null);
  // null 表示新会话或旧会话尚未保存过能力偏好；空数组则表示用户明确取消了全部能力。
  const capabilitySelectionRef = useRef<{
    mcpServerIds: string[];
    skillIds: string[];
  } | null>(null);
  const catalogLoadedRef = useRef(false);

  function updateComposerInput(value: string) {
    composerInputRef.current = value;
    setInput(value);
  }

  useEffect(() => {
    void refreshSessions();
    void refreshHealth();
    void refreshOptions();
    const savedId = localStorage.getItem(appConfig.storageKey);
    if (savedId) void openSession(savedId);
    return () => {
      activeRunsRef.current.forEach((run) => run.controller.abort());
      activeRunsRef.current.clear();
      if (copyFeedbackTimerRef.current !== null) {
        window.clearTimeout(copyFeedbackTimerRef.current);
      }
      voiceRecognitionStopReasonRef.current = "exit";
      speechRef.current?.stop();
      speechRef.current = null;
      cancelVoiceEngine();
    };
  }, []);

  useEffect(() => {
    voiceAutoEnabledRef.current = voicePlayback.autoEnabled;
  }, [voicePlayback.autoEnabled]);

  useEffect(() => {
    voiceConversationActiveRef.current = voiceConversation.active;
    voiceInputMutedRef.current = voiceConversation.inputMuted;
  }, [voiceConversation.active, voiceConversation.inputMuted]);

  useEffect(() => {
    const handleShortcut = (event: globalThis.KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        startNewSession();
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [loading]);

  useEffect(() => {
    if (!streamPinnedRef.current) return;
    streamEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages, tools, approvals, loading]);

  useEffect(() => {
    if (view !== "chat" || !streamPinnedRef.current) return;
    // The chat DOM is absent while ConfigCenter is open. Scroll after it mounts
    // so events received in the background are visible immediately on return.
    const frameId = window.requestAnimationFrame(() => {
      streamEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [view]);

  useEffect(() => {
    const textarea = composerTextareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${textarea.scrollHeight}px`;
  }, [input, sidebarWidth, inspectorWidth, sidebarCollapsed, inspectorOpen]);

  useEffect(() => {
    document.documentElement.dataset.theme = colorTheme;
    localStorage.setItem("k-agent-color-theme", colorTheme);
  }, [colorTheme]);

  useEffect(() => {
    document.documentElement.style.setProperty(`--${"f" + "s"}-delta`, `${fontSizeDelta}px`);
    localStorage.setItem("k-agent-font-size-delta", String(fontSizeDelta));
  }, [fontSizeDelta]);

  useEffect(() => {
    if (!fontSizePopoverOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest(".font-size-control")) {
        setFontSizePopoverOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [fontSizePopoverOpen]);

  useEffect(() => {
    localStorage.setItem("k-agent-sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  useEffect(() => {
    // Only the scheduled-task surface survives a full reload. Config and Team
    // keep their existing explicit back-navigation semantics.
    localStorage.setItem(ACTIVE_VIEW_STORAGE_KEY, view === "scheduled" ? "scheduled" : "chat");
  }, [view]);

  useEffect(() => {
    localStorage.setItem("k-agent-desktop-pet-enabled", String(desktopPetEnabled));
  }, [desktopPetEnabled]);

  useEffect(() => {
    localStorage.setItem("k-agent-right-panel-mode", rightPanelMode);
  }, [rightPanelMode]);

  useEffect(() => {
    workspaceCacheRef.current.clear();
    selectedWorkspacePathRef.current = null;
    setWorkspaceItems([]);
    setSelectedWorkspacePath(null);
    setWorkspaceError(null);
  }, [sessionId]);

  useEffect(() => {
    if (rightPanelMode !== "workspace" || !sessionId) {
      return;
    }

    let cancelled = false;

    async function refreshWorkspace() {
      const activeId = sessionId;
      if (!activeId) return;
      setWorkspaceLoading(true);
      setWorkspaceError(null);
      try {
        const listing = await getSessionWorkspace(activeId);
        if (cancelled) return;

        let nextSelected = selectedWorkspacePathRef.current;
        if (!nextSelected || !listing.files.some((file) => file.path === nextSelected)) {
          nextSelected = listing.files[0]?.path ?? null;
        }
        selectedWorkspacePathRef.current = nextSelected;

        const items: ContentStageItem[] = [];
        for (const file of listing.files) {
          const cacheKey = `${activeId}:${file.path}`;
          // Always refresh the selected file so Agent writes show up live.
          const shouldFetch = file.path === nextSelected
            || (!workspaceCacheRef.current.has(cacheKey) && listing.files.length <= 12);
          let content = workspaceCacheRef.current.get(cacheKey);
          if (shouldFetch) {
            try {
              const payload = await getSessionWorkspaceFile(activeId, file.path);
              if (cancelled) return;
              content = formatWorkspacePreview(payload);
              workspaceCacheRef.current.set(cacheKey, content);
            } catch {
              content = "读取失败";
              workspaceCacheRef.current.set(cacheKey, content);
            }
          }
          items.push({
            id: file.path,
            title: file.name,
            subtitle: file.path,
            badge: formatBytes(file.size),
            content: content ?? "选择后加载预览…"
          });
        }
        setWorkspaceItems(items);
        setSelectedWorkspacePath(nextSelected);
      } catch (reason) {
        if (!cancelled) {
          setWorkspaceItems([]);
          setWorkspaceError(reason instanceof Error ? reason.message : "工作区加载失败");
        }
      } finally {
        if (!cancelled) setWorkspaceLoading(false);
      }
    }

    void refreshWorkspace();
    const timer = window.setInterval(() => {
      void refreshWorkspace();
    }, loading ? 2500 : 8000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [rightPanelMode, sessionId, loading]);

  async function openWorkspaceFile(path: string) {
    if (!sessionId) return;
    selectedWorkspacePathRef.current = path;
    setSelectedWorkspacePath(path);
    await resolveWorkspaceAsset(path);
  }

  async function resolveWorkspaceAsset(path: string): Promise<string | null> {
    if (!sessionId) return null;
    const cacheKey = `${sessionId}:${path}`;
    const cached = workspaceCacheRef.current.get(cacheKey);
    if (cached && cached !== "选择后加载预览…" && cached !== "读取失败") {
      return cached;
    }
    try {
      const payload = await getSessionWorkspaceFile(sessionId, path);
      const content = formatWorkspacePreview(payload);
      workspaceCacheRef.current.set(cacheKey, content);
      setWorkspaceItems((current) => {
        if (!current.some((item) => item.id === path)) return current;
        return current.map((item) => (item.id === path ? { ...item, content } : item));
      });
      return content;
    } catch {
      workspaceCacheRef.current.set(cacheKey, "读取失败");
      setWorkspaceItems((current) => current.map((item) => (
        item.id === path ? { ...item, content: "读取失败" } : item
      )));
      return null;
    }
  }

  const filteredSessions = sessions.filter((session) =>
    session.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  );
  const activeSession = sessions.find((session) => session.id === sessionId);
  const groupedMessages = groupDisplayMessages(messages);
  const activityTurnIds = new Set([
    ...thinkingBlocks.map((block) => block.turnId),
    ...tools.map((tool) => tool.turnId),
    ...approvals.map((approval) => approval.runId),
    ...textBlocks.map((text) => text.turnId)
  ]);
  const displayMessages = groupedMessages.filter((message, index) => {
    if (message.role !== "assistant") return true;
    if (message.content.trim()) return true;
    if (message.meta?.runId && activityTurnIds.has(message.meta.runId)) return true;
    return loading && index === groupedMessages.length - 1;
  });

  async function refreshHealth() {
    try {
      setHealth(await getHealth());
    } catch {
      setHealth(null);
    }
  }

  async function refreshSessions() {
    try {
      const persistedSessions = normalizeSessionSummaries(await listSessions());
      setSessions((current) => {
        // A just-started run can predate the first session-store write by a
        // fraction of a second. Preserve its optimistic sidebar row meanwhile.
        const persistedIds = new Set(persistedSessions.map((session) => session.id));
        const optimisticRuns = current.filter(
          (session) => activeRunsRef.current.has(session.id) && !persistedIds.has(session.id)
        );
        return [...optimisticRuns, ...persistedSessions];
      });
    } catch {
      // A transient list failure must not erase navigation back to local runs.
      setSessions((current) => current.filter((session) => activeRunsRef.current.has(session.id)));
    }
  }

  async function refreshOptions() {
    try {
      const [modelData, catalog, agentsCatalog] = await Promise.all([
        getModelsConfig(), getRuntimeCatalog(), getAgentsCatalog()
      ]);
      const enabledModels = modelData.models.filter((model) => model.enabled);
      const enabledMcp = catalog.mcpServers.filter((server) => server.enabled);
      const enabledSkills = catalog.skills.filter((skill) => skill.enabled);
      const availableAgents = agentsCatalog.agents;
      setModels(enabledModels);
      setAgents(availableAgents.length ? availableAgents : [{ kind: "k_agent", name: "K Agent", available: true }]);
      setMcpServers(enabledMcp);
      setSkills(enabledSkills);
      catalogLoadedRef.current = true;
      const selectable = availableAgents.filter((agent) => agent.available);
      const nextKind = selectable.some((agent) => agent.kind === agentKind)
        ? agentKind
        : (agentsCatalog.defaultKind || "k_agent");
      localStorage.setItem(AGENT_KIND_STORAGE_KEY, nextKind);
      setAgentKind(nextKind);
      const nextModel = resolveModelForAgent(nextKind, enabledModels, availableAgents);
      setModelId(nextModel);
      if (nextModel) writeModelForAgent(nextKind, nextModel);
      const remembered = capabilitySelectionRef.current;
      const nextMcp = remembered
        ? remembered.mcpServerIds.filter((id) => enabledMcp.some((server) => server.id === id))
        : enabledMcp.map((server) => server.id);
      const nextSkills = remembered
        ? remembered.skillIds.filter((id) => enabledSkills.some((skill) => skill.id === id))
        : [];
      setSelectedMcp(nextMcp);
      setSelectedSkills(nextSkills);
      if (remembered) {
        capabilitySelectionRef.current = {
          mcpServerIds: nextMcp,
          skillIds: nextSkills
        };
      }
    } catch {
      catalogLoadedRef.current = false;
      setModels([]);
      setAgents([{ kind: "k_agent", name: "K Agent", available: true }]);
      setAgentKind("k_agent");
      setMcpServers([]);
      setSkills([]);
    }
  }

  /**
   * 打开历史会话：加载快照 →（可选）重放 events 重建时间线 → 拼接切会话期间缓冲的流式尾部。
   * navigationToken 丢弃过期异步结果；bufferedRun 表示该会话仍有未结束的本地 run。
   */
  async function openSession(nextId: string) {
    stopVoiceConversation("切换会话，语音会话已结束");
    const navigationToken = sessionNavigationTokenRef.current + 1;
    sessionNavigationTokenRef.current = navigationToken;
    const bufferedRun = activeRunsRef.current.get(nextId);
    const bufferedEventCount = bufferedRun?.events.length ?? 0;
    activeSessionIdRef.current = null;
    setSessionId(nextId);
    resetRunViewState();
    setSessionLoading(true);
    setLoading(Boolean(bufferedRun));
    setStatus(bufferedRun ? appConfig.status.processing : "正在加载会话");
    setSidebarOpen(false);
    try {
      const data = await getSession(nextId);
      if (sessionNavigationTokenRef.current !== navigationToken) return;
      setSessionId(data.sessionId);
      activeSessionIdRef.current = data.sessionId;
      localStorage.setItem(appConfig.storageKey, data.sessionId);
      const remembered = data.capabilities ?? null;
      capabilitySelectionRef.current = remembered;
      if (remembered) {
        setSelectedMcp(
          catalogLoadedRef.current
            ? remembered.mcpServerIds.filter((id) => mcpServers.some((server) => server.id === id))
            : remembered.mcpServerIds
        );
        setSelectedSkills(
          catalogLoadedRef.current
            ? remembered.skillIds.filter((id) => skills.some((skill) => skill.id === id))
            : remembered.skillIds
        );
      } else {
        // 旧会话没有 capabilities；即使目录请求仍在进行，也应保持“默认选择”
        // 语义，让随后完成的 refreshOptions 应用当前全部可用 MCP。
        setSelectedMcp(mcpServers.map((server) => server.id));
        setSelectedSkills([]);
      }
      resetRunViewState();
      const normalizedMessages = normalizeMessages(data.messages);
      persistedMessageIdsRef.current = new Set(normalizedMessages.map((message) => message.id));
      const historicalEvents = Array.isArray(data.events) ? data.events : [];
      // messages 是紧凑模型上下文；失败 run 可能只在 events 里，需合成错误气泡。
      setMessages(mergeHistoricalMessages(
        normalizedMessages,
        historicalEvents,
        appConfig.text.requestErrorPrefix
      ));
      if (Array.isArray(data.events) && data.events.length) {
        setTasks(normalizeStringList(data.tasks));
        // 重放时跳过已持久化文本气泡，避免与 messages 重复；仍重建 thinking/tools。
        replayingHistoryRef.current = true;
        data.events.forEach((event) => applyAgUiEvent(event));
        replayingHistoryRef.current = false;
      }
      if (!Array.isArray(data.events) || !data.events.length) {
        setTrace(normalizeStringList(data.trace));
        setTasks(normalizeStringList(data.tasks));
        setThinking(normalizeThinking(data.thinking));
      }
      // getSession 飞行期间到达的事件比快照新；只重放尾部，避免缺口与重复 delta。
      bufferedRun?.events.slice(bufferedEventCount).forEach((event) => applyAgUiEvent(event));
      const isRunning = activeRunsRef.current.has(data.sessionId);
      setSessionLoading(false);
      setLoading(isRunning);
      setStatus(isRunning ? appConfig.status.processing : appConfig.status.idle);
    } catch {
      if (sessionNavigationTokenRef.current !== navigationToken) return;
      const localRun = activeRunsRef.current.get(nextId);
      if (localRun) {
        // RUN_STARTED 可能尚未落库；用本地事件缓冲重建进行中视图。
        resetRunViewState();
        setMessages(localRun.initialMessages);
        pendingAssistantIdRef.current = localRun.pendingAssistantId;
        activeSessionIdRef.current = nextId;
        localRun.events.forEach((event) => applyAgUiEvent(event));
        setSessionLoading(false);
        setLoading(true);
        setStatus(appConfig.status.processing);
        localStorage.setItem(appConfig.storageKey, nextId);
        return;
      }
      localStorage.removeItem(appConfig.storageKey);
      setSessionId(null);
      setSessionLoading(false);
      setLoading(false);
      setStatus("会话已失效");
    }
  }

  /** 清空当前线程指针与视图；不中止其他会话上仍在跑的 activeRuns。 */
  function startNewSession() {
    stopVoiceConversation("新会话已准备，语音会话已结束");
    sessionNavigationTokenRef.current += 1;
    activeSessionIdRef.current = null;
    localStorage.removeItem(appConfig.storageKey);
    setSessionId(null);
    capabilitySelectionRef.current = null;
    setSelectedMcp(mcpServers.map((server) => server.id));
    setSelectedSkills([]);
    persistedMessageIdsRef.current = new Set();
    resetRunViewState();
    setSessionLoading(false);
    setLoading(false);
    setStatus(appConfig.status.idle);
    setSidebarOpen(false);
  }

  function resetRunViewState() {
    setMessages([]);
    setTrace([]);
    setTasks([]);
    setThinking([]);
    setThinkingBlocks([]);
    setTextBlocks([]);
    setApprovals([]);
    setActiveRunId(null);
    activeRunIdRef.current = null;
    pendingAssistantIdRef.current = null;
    activeThinkingBlockIdRef.current = null;
    activeThinkingStepIdRef.current = null;
    activeTextActivityIdRef.current = null;
    activeTextMessageIdRef.current = null;
    assistantVisibleTextLengthRef.current = 0;
    activitySequenceRef.current = 0;
    setTools([]);
  }

  function stopRun() {
    if (!sessionId) return;
    const activeRun = activeRunsRef.current.get(sessionId);
    activeRun?.controller.abort();
    if (activeRun) {
      // 主动停止与 Access Layer 回滚一致：去掉本轮 user + 占位 assistant，并通知服务端 cancel。
      const removableMessageIds = new Set([
        activeRun.userMessageId,
        activeRun.pendingAssistantId,
        ...activeRun.events.flatMap((event) =>
          event.type === "TEXT_MESSAGE_START" || event.type === "TEXT_MESSAGE_CONTENT" || event.type === "TEXT_MESSAGE_END"
            ? [event.messageId]
            : []
        )
      ]);
      setMessages((current) => current.filter((message) => !removableMessageIds.has(message.id)));
      setTextBlocks((current) => current.filter((text) => !removableMessageIds.has(text.id)));
      void cancelSessionRun(sessionId, activeRun.runId)
        .then(() => refreshSessions())
        .catch(() => refreshSessions());
    }
    stopVoicePlayback("已停止生成和朗读");
    voiceRunFinishedRef.current = true;
    if (voiceConversationActiveRef.current) {
      setVoiceConversation((current) => ({
        ...current,
        phase: current.inputMuted ? "inactive" : "listening",
        message: current.inputMuted ? "麦克风已暂停" : "正在聆听"
      }));
      window.setTimeout(() => startVoiceRecognition(), 0);
    }
    setLoading(false);
    setStatus("已停止");
  }

  function changeSelectedMcp(ids: string[]) {
    setSelectedMcp(ids);
    capabilitySelectionRef.current = {
      mcpServerIds: ids,
      skillIds: selectedSkills
    };
  }

  function changeSelectedSkills(ids: string[]) {
    setSelectedSkills(ids);
    capabilitySelectionRef.current = {
      mcpServerIds: selectedMcp,
      skillIds: ids
    };
  }

  async function copyMessage(messageId: string, content: string) {
    if (copyFeedbackTimerRef.current !== null) {
      window.clearTimeout(copyFeedbackTimerRef.current);
    }

    try {
      // Clipboard API needs a secure context (https / localhost). LAN http://IP
      // falls back to a hidden textarea + execCommand so intranet copies still work.
      if (window.isSecureContext && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(content);
      } else {
        const area = document.createElement("textarea");
        area.value = content;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.left = "-9999px";
        area.style.top = "0";
        document.body.appendChild(area);
        area.select();
        area.setSelectionRange(0, area.value.length);
        const ok = document.execCommand("copy");
        document.body.removeChild(area);
        if (!ok) throw new Error("execCommand copy failed");
      }
      setCopyFeedback({ messageId, status: "success" });
    } catch {
      setCopyFeedback({ messageId, status: "error" });
    }

    copyFeedbackTimerRef.current = window.setTimeout(() => {
      setCopyFeedback((current) => current?.messageId === messageId ? null : current);
      copyFeedbackTimerRef.current = null;
    }, 1800);
  }

  function stopVoicePlayback(message = "朗读已停止") {
    cancelVoiceEngine();
    setVoicePlayback((current) => ({
      ...current,
      status: current.supported ? "idle" : "unsupported",
      message,
      messageId: undefined
    }));
  }

  function cancelVoiceEngine() {
    voiceGenerationRef.current += 1;
    voiceStoppingRef.current = true;
    voiceSpeakingRef.current = false;
    voiceSessionIdRef.current = null;
    voiceMessageIdRef.current = null;
    voiceBufferRef.current = "";
    voiceQueueRef.current = [];
    voiceEchoReferenceTextRef.current = "";
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    window.setTimeout(() => {
      voiceStoppingRef.current = false;
    }, 0);
  }

  function beginVoiceReply(sessionId: string, messageId: string) {
    if (voiceConversationActiveRef.current) {
      voiceRunFinishedRef.current = false;
      setVoiceConversation((current) => ({ ...current, phase: "processing", message: "正在等待助手回复" }));
    }
    if (!voiceAutoEnabledRef.current || !voicePlayback.supported) return;
    // Speech synthesis is browser-global. Resetting before a new run prevents
    // an older assistant turn from continuing over the next real-time reply.
    stopVoicePlayback("等待助手回复");
    voiceSessionIdRef.current = sessionId;
    voiceMessageIdRef.current = messageId;
    setVoicePlayback((current) => ({
      ...current,
      status: "waiting",
      message: "正在等待可朗读内容",
      messageId
    }));
  }

  function handleVoiceStreamEvent(event: AgUiEvent, eventSessionId: string) {
    // Once a user barges in, late events from the aborted turn remain available
    // to the normal AG-UI renderer but must never restart its speech queue.
    if (voiceBargeInActiveRef.current) return;
    // Conversation turn-taking depends on the run boundary even when the user
    // muted TTS, so RUN_FINISHED must not be hidden behind the output guard.
    if (event.type === "RUN_FINISHED") {
      voiceRunFinishedRef.current = true;
      maybeResumeVoiceListening();
      return;
    }
    if (event.type === "RUN_ERROR") {
      voiceRunFinishedRef.current = true;
      stopVoicePlayback("助手回复出错，朗读已停止");
      if (voiceConversationActiveRef.current) {
        setVoiceConversation((current) => ({ ...current, phase: "error", message: "回复出错，可以继续说话重试" }));
        // Keep the error visible briefly, then return to hands-free listening.
        window.setTimeout(() => maybeResumeVoiceListening(), 1200);
      }
      return;
    }
    if (!voiceAutoEnabledRef.current || !voicePlayback.supported || replayingHistoryRef.current) return;

    if (event.type === "TEXT_MESSAGE_START") {
      voiceSessionIdRef.current = eventSessionId;
      voiceMessageIdRef.current = event.messageId;
      voiceBufferRef.current = "";
      setVoicePlayback((current) => ({
        ...current,
        status: "waiting",
        message: "正在接收助手回复",
        messageId: event.messageId
      }));
      return;
    }

    if (!voiceSessionIdRef.current) {
      voiceSessionIdRef.current = eventSessionId;
    }
    if (voiceSessionIdRef.current !== eventSessionId) return;

    if (event.type === "TEXT_MESSAGE_CONTENT" && !voiceMessageIdRef.current) {
      // Reattaching to an already-running session may miss TEXT_MESSAGE_START
      // locally; resume from the next live delta without replaying buffered text.
      voiceMessageIdRef.current = event.messageId;
      setVoicePlayback((current) => ({
        ...current,
        status: "waiting",
        message: "正在接收助手回复",
        messageId: event.messageId
      }));
    }

    if (event.type === "TEXT_MESSAGE_CONTENT" && voiceMessageIdRef.current === event.messageId) {
      appendVoiceDelta(event.delta);
      return;
    }

    if (event.type === "TEXT_MESSAGE_END" && voiceMessageIdRef.current === event.messageId) {
      flushVoiceBuffer(true);
      voiceMessageIdRef.current = null;
      if (!voiceSpeakingRef.current && voiceQueueRef.current.length === 0) {
        setVoicePlayback((current) => ({
          ...current,
          status: "idle",
          message: "朗读完成",
          messageId: undefined
        }));
      }
      return;
    }

  }

  function appendVoiceDelta(delta: string) {
    voiceBufferRef.current += delta;
    flushVoiceBuffer(false);
  }

  function flushVoiceBuffer(force: boolean) {
    const chunks = splitSpeakableChunks(voiceBufferRef.current, force);
    voiceBufferRef.current = chunks.remainder;
    const cleaned = chunks.ready
      .map((chunk) => sanitizeSpeechText(chunk))
      .filter((chunk) => chunk.length > 0);
    if (!cleaned.length) return;
    voiceQueueRef.current.push(...cleaned);
    speakNextVoiceChunk(voiceGenerationRef.current);
  }

  function speakNextVoiceChunk(generation: number) {
    if (!voicePlayback.supported || voiceSpeakingRef.current || voiceQueueRef.current.length === 0) return;
    const text = voiceQueueRef.current.shift();
    if (!text) return;
    // Keep several recent chunks because recognition can return speaker echo
    // spanning a sentence boundary after synthesis has advanced to the next one.
    voiceEchoReferenceTextRef.current = `${voiceEchoReferenceTextRef.current} ${text}`.slice(-800);
    const utterance = new SpeechSynthesisUtterance(text);
    // Voice catalogs are device-owned and may load after the app. A stale URI
    // falls back to the browser default without interrupting streamed playback.
    const selectedVoice = window.speechSynthesis
      .getVoices()
      .find((voice) => voice.voiceURI === voiceConfig.voiceURI);
    if (selectedVoice) {
      utterance.voice = selectedVoice;
      utterance.lang = selectedVoice.lang;
    } else {
      utterance.lang = /[\u4e00-\u9fff]/.test(text) ? "zh-CN" : "en-US";
    }
    utterance.rate = voiceConfig.rate;
    utterance.pitch = voiceConfig.pitch;
    utterance.volume = voiceConfig.volume;
    voiceSpeakingRef.current = true;
    utterance.onstart = () => {
      if (voiceGenerationRef.current !== generation) return;
      setVoicePlayback((current) => ({
        ...current,
        status: "speaking",
        message: "正在朗读助手回复",
        messageId: voiceMessageIdRef.current ?? current.messageId
      }));
      if (voiceConversationActiveRef.current) {
        setVoiceConversation((current) => ({ ...current, phase: "speaking", message: "助手正在说话，你可以直接说话打断" }));
        // Barge-in recognition stays active during playback. Transcript echo is
        // filtered before cancellation, so speaker output does not immediately
        // interrupt itself on browsers without reliable acoustic echo control.
        window.setTimeout(() => startVoiceRecognition("barge-in"), 120);
      }
    };
    utterance.onend = () => {
      if (voiceGenerationRef.current !== generation) return;
      voiceSpeakingRef.current = false;
      if (voiceQueueRef.current.length > 0) {
        speakNextVoiceChunk(generation);
        return;
      }
      setVoicePlayback((current) => ({
        ...current,
        status: "idle",
        message: voiceBufferRef.current.trim() ? "等待更多内容" : "朗读完成",
        messageId: voiceMessageIdRef.current ?? undefined
      }));
      maybeResumeVoiceListening();
    };
    utterance.onerror = () => {
      voiceSpeakingRef.current = false;
      if (voiceStoppingRef.current || voiceGenerationRef.current !== generation) return;
      voiceQueueRef.current = [];
      setVoicePlayback((current) => ({
        ...current,
        status: "error",
        message: "语音输出失败，请检查浏览器语音权限或系统声音",
        messageId: undefined
      }));
      setStatus("语音输出失败");
      if (voiceConversationActiveRef.current) {
        setVoiceConversation((current) => ({ ...current, phase: "error", message: "语音输出失败" }));
      }
    };
    window.speechSynthesis.speak(utterance);
  }

  function speakMessage(messageId: string, content: string) {
    if (!voicePlayback.supported) {
      setStatus("当前浏览器不支持语音输出");
      return;
    }
    stopVoicePlayback("准备朗读消息");
    voiceGenerationRef.current += 1;
    voiceMessageIdRef.current = messageId;
    voiceQueueRef.current = splitTextForManualSpeech(content);
    if (!voiceQueueRef.current.length) {
      setVoicePlayback((current) => ({
        ...current,
        status: "idle",
        message: "没有可朗读内容",
        messageId: undefined
      }));
      return;
    }
    setVoicePlayback((current) => ({
      ...current,
      status: "waiting",
      message: "准备朗读消息",
      messageId
    }));
    speakNextVoiceChunk(voiceGenerationRef.current);
  }

  function maybeResumeVoiceListening() {
    if (
      !voiceConversationActiveRef.current
      || voiceInputMutedRef.current
      || !voiceRunFinishedRef.current
      || voiceSpeakingRef.current
      || voiceQueueRef.current.length > 0
      || voiceBufferRef.current.trim()
      || voiceMessageIdRef.current
    ) return;
    setVoiceConversation((current) => ({ ...current, phase: "listening", message: "正在聆听" }));
    window.setTimeout(() => startVoiceRecognition(), 180);
  }

  function beginResize(side: "sidebar" | "inspector", event: ReactPointerEvent<HTMLDivElement>) {
    if (window.innerWidth <= 860) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = side === "sidebar" ? sidebarWidth : inspectorWidth;
    let latestWidth = startWidth;
    document.body.classList.add("resizing-layout");

    const handleMove = (moveEvent: globalThis.PointerEvent) => {
      const delta = moveEvent.clientX - startX;
      const nextWidth = side === "sidebar"
        ? Math.min(380, Math.max(190, startWidth + delta))
        : Math.min(560, Math.max(280, startWidth - delta));
      latestWidth = nextWidth;
      if (side === "sidebar") setSidebarWidth(nextWidth);
      else setInspectorWidth(nextWidth);
    };
    const handleEnd = () => {
      document.body.classList.remove("resizing-layout");
      localStorage.setItem(
        side === "sidebar" ? "k-agent-sidebar-width" : "k-agent-inspector-width",
        String(latestWidth)
      );
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleEnd);
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleEnd);
  }

  function resizeWithKeyboard(side: "sidebar" | "inspector", event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    if (side === "sidebar") {
      const next = Math.min(380, Math.max(190, sidebarWidth + direction * 10));
      setSidebarWidth(next);
      localStorage.setItem("k-agent-sidebar-width", String(next));
    } else {
      const next = Math.min(560, Math.max(280, inspectorWidth - direction * 10));
      setInspectorWidth(next);
      localStorage.setItem("k-agent-inspector-width", String(next));
    }
  }

  function handleMessageStreamScroll() {
    const element = messageStreamRef.current;
    if (!element) return;
    const distanceToBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
    streamPinnedRef.current = distanceToBottom < 36;
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = voiceAutoSubmitPromptRef.current ?? input.trim();
    voiceAutoSubmitPromptRef.current = null;
    if (voiceConversationActiveRef.current && speechRef.current) {
      // Keyboard submission during listening must retire that recognition
      // instance, otherwise its later onend could submit the same turn again.
      voiceRecognitionStopReasonRef.current = "submit";
      speechRef.current.stop();
    }
    void submitConversation(prompt);
  }

  /**
   * 提交一轮对话：乐观插入 user + 占位 assistant，组装 AgUiRunInput，再挂到 activeRuns 并开 SSE。
   * 仅当前可见会话会把流事件应用到 UI；切走时仍写入 activeRun.events 供回来时重放。
   */
  async function submitConversation(prompt: string) {
    if ((!prompt && !attachments.length) || loading || sessionLoading || !modelId) {
      if (voiceConversationActiveRef.current) {
        voiceRunFinishedRef.current = !loading;
        setVoiceConversation((current) => ({
          ...current,
          phase: loading || sessionLoading ? "processing" : "error",
          message: loading || sessionLoading
            ? "当前回复尚未结束"
            : !modelId ? "请先选择可用模型" : "没有识别到可发送内容"
        }));
      }
      return;
    }

    // 新用户回合接管后续语音事件，并解除打断后对迟到 delta 的抑制。
    voiceBargeInActiveRef.current = false;

    const agentModels = modelsForAgent(agentKind, models, agents);
    const selectedModel = agentModels.find((model) => model.id === modelId);
    const validReasoningOptions = reasoningOptionsForModel(selectedModel);
    const effectiveReasoningEffort = validReasoningOptions.some((option) => option.id === reasoningEffort)
      ? reasoningEffort
      : "none";
    const runId = createClientId();
    const pendingAssistantId = createClientId();
    const userMessage = createMessage("user", prompt);
    const nextMessages = [...messages, userMessage, createStreamingMessage(pendingAssistantId)];
    const targetSessionId = sessionId ?? createClientId();
    // Access Layer 只收本轮 user 消息；完整历史由服务端按 threadId 拼接。
    // 模型/MCP/skills/agent 选项走 forwardedProps，不进入 messages 正文。
    const runInput: AgUiRunInput = {
      threadId: targetSessionId,
      runId,
      state: {},
      messages: [
        {
          id: userMessage.id,
          role: userMessage.role,
          content: userMessage.content
        }
      ],
      tools: [],
      context: [],
      forwardedProps: {
        modelId,
        mcpServerIds: selectedMcp,
        skillIds: selectedSkills,
        reasoningEffort: effectiveReasoningEffort,
        attachments,
        agentKind,
        // 语音风格是 run 级元数据，不写进 prompt，保证落库 transcript 与用户原话一致。
        agentOptions: {
          voiceConversation: voiceConversationActiveRef.current,
          voiceStyle: voiceConfig.style,
          ...(agentKind === "k_agent" ? {} : { cliSessionMode })
        }
      }
    };

    const controller = new AbortController();
    const activeRun: ActiveConversationRun = {
      controller,
      events: [],
      initialMessages: nextMessages,
      pendingAssistantId,
      runId,
      userMessageId: userMessage.id
    };
    activeRunsRef.current.set(targetSessionId, activeRun);
    activeSessionIdRef.current = targetSessionId;
    setRunningSessionIds((current) => new Set(current).add(targetSessionId));
    setSessionId(targetSessionId);
    localStorage.setItem(appConfig.storageKey, targetSessionId);
    setSessions((current) => current.some((session) => session.id === targetSessionId)
      ? current
      : [
        {
          id: targetSessionId,
          title: prompt || attachments[0]?.name || "新会话",
          updatedAt: new Date().toISOString(),
          messageCount: nextMessages.length
        },
        ...current
      ]);
    streamPinnedRef.current = true;
    setMessages(nextMessages);
    pendingAssistantIdRef.current = pendingAssistantId;
    updateComposerInput("");
    setAttachments([]);
    beginVoiceReply(targetSessionId, pendingAssistantId);
    activeThinkingBlockIdRef.current = null;
    activeThinkingStepIdRef.current = null;
    activeTextActivityIdRef.current = null;
    activeTextMessageIdRef.current = null;
    assistantVisibleTextLengthRef.current = 0;
    setLoading(true);
    setStatus(appConfig.status.preparing);

    try {
      await streamAgentRun(
        runInput,
        (streamEvent) => {
          activeRun.events.push(streamEvent);
          if (activeSessionIdRef.current === targetSessionId) {
            applyAgUiEvent(streamEvent);
            handleVoiceStreamEvent(streamEvent, targetSessionId);
          }
        },
        controller.signal
      );
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      const detail = error instanceof Error && error.message.trim()
        ? error.message
        : (typeof error === "string" && error.trim() ? error : "服务未返回错误详情，请查看后端日志");
      const errorEvent: AgUiEvent = { type: "RUN_ERROR", message: detail };
      activeRun.events.push(errorEvent);
      if (activeSessionIdRef.current === targetSessionId) {
        applyAgUiEvent(errorEvent);
        handleVoiceStreamEvent(errorEvent, targetSessionId);
      }
    } finally {
      // 仅清除本 controller 拥有的 run，避免后发起的同会话 run 被误删。
      const stillOwnsSession = activeRunsRef.current.get(targetSessionId)?.controller === controller;
      if (stillOwnsSession) {
        activeRunsRef.current.delete(targetSessionId);
        setRunningSessionIds((current) => {
          const next = new Set(current);
          next.delete(targetSessionId);
          return next;
        });
        if (activeSessionIdRef.current === targetSessionId) {
          setLoading(false);
        }
      }
      await Promise.all([refreshSessions(), refreshHealth()]);
    }
  }

  /**
   * 将单条 AG-UI 事件投影到 React 状态（messages / thinking / tools / approvals / status）。
   * 状态机大致：idle → preparing → processing（文本/工具/审批）→ complete | failed。
   * replayingHistoryRef 为 true 时避免把已持久化消息再 append 一遍。
   */
  function applyAgUiEvent(event: AgUiEvent) {
    switch (event.type) {
      case "RUN_STARTED":
        activeRunIdRef.current = event.runId;
        setActiveRunId(event.runId);
        setSessionId(event.threadId);
        localStorage.setItem(appConfig.storageKey, event.threadId);
        setStatus(appConfig.status.processing);
        if (!replayingHistoryRef.current) {
          const pendingAssistantId = pendingAssistantIdRef.current;
          if (pendingAssistantId) {
            setMessages((current) => current.map((message) =>
              message.id === pendingAssistantId
                ? { ...message, meta: { ...message.meta, runId: event.runId } }
                : message
            ));
          }
        }
        break;
      case "TEXT_MESSAGE_START":
      {
        const textTurnId = activeRunIdRef.current ?? event.messageId;
        const textSequence = activitySequenceRef.current + 1;
        activitySequenceRef.current = textSequence;
        activeTextMessageIdRef.current = event.messageId;
        assistantVisibleTextLengthRef.current = 0;
        activeTextActivityIdRef.current = event.messageId;
        setTextBlocks((current) => [
          ...current,
          {
            id: event.messageId,
            turnId: textTurnId,
            content: "",
            status: "streaming",
            sequence: textSequence
          }
        ]);
        if (replayingHistoryRef.current && persistedMessageIdsRef.current.has(event.messageId)) {
          break;
        }
        // 占位 assistant id 换成服务端 messageId，后续 CONTENT delta 按该 id 拼接。
        setMessages((current) => {
          const pendingAssistantId = pendingAssistantIdRef.current;
          const fallbackPendingId = [...current].reverse().find((message) => message.role === "assistant" && !message.content)?.id;
          const reusableAssistantId = pendingAssistantId && current.some((message) => message.id === pendingAssistantId)
            ? pendingAssistantId
            : fallbackPendingId;
          if (reusableAssistantId) {
            pendingAssistantIdRef.current = null;
            return current.map((message) =>
              message.id === reusableAssistantId
                ? {
                  ...message,
                  id: event.messageId,
                  meta: { ...message.meta, runId: textTurnId }
                }
                : message
            );
          }
          return current.some((message) => message.id === event.messageId)
            ? current
            : [...current, createStreamingMessage(event.messageId, textTurnId)];
        });
        break;
      }
      case "TEXT_MESSAGE_END":
        if (activeTextMessageIdRef.current === event.messageId) {
          activeTextMessageIdRef.current = null;
        }
        if (activeTextActivityIdRef.current === event.messageId) {
          activeTextActivityIdRef.current = null;
        }
        setTextBlocks((current) => current.map((text) =>
          text.id === event.messageId ? { ...text, status: "complete" } : text
        ));
        break;
      case "TEXT_MESSAGE_CONTENT":
      {
        const textTurnId = activeRunIdRef.current ?? event.messageId;
        {
          const visibleDeltaLength = visibleTextLength(event.delta);
          assistantVisibleTextLengthRef.current += visibleDeltaLength;
        }
        setTextBlocks((current) => {
          if (current.some((text) => text.id === event.messageId)) {
            return current.map((text) =>
              text.id === event.messageId ? { ...text, content: text.content + event.delta } : text
            );
          }
          activitySequenceRef.current += 1;
          activeTextActivityIdRef.current = event.messageId;
          return [
            ...current,
            {
              id: event.messageId,
              turnId: textTurnId,
              content: event.delta,
              status: "streaming",
              sequence: activitySequenceRef.current
            }
          ];
        });
        if (replayingHistoryRef.current && persistedMessageIdsRef.current.has(event.messageId)) {
          break;
        }
        setMessages((current) => {
          if (current.some((message) => message.id === event.messageId)) {
            return current.map((message) =>
              message.id === event.messageId
                ? { ...message, content: message.content + event.delta }
                : message
            );
          }
          const pendingAssistantId = pendingAssistantIdRef.current;
          const fallbackPendingId = [...current].reverse().find((message) => message.role === "assistant" && !message.content)?.id;
          const reusableAssistantId = pendingAssistantId && current.some((message) => message.id === pendingAssistantId)
            ? pendingAssistantId
            : fallbackPendingId;
          pendingAssistantIdRef.current = null;
          return reusableAssistantId
            ? current.map((message) =>
              message.id === reusableAssistantId
                ? {
                  ...message,
                  id: event.messageId,
                  content: event.delta,
                  meta: { ...message.meta, runId: textTurnId }
                }
                : message
            )
            : [
              ...current,
              {
                ...createStreamingMessage(event.messageId, textTurnId),
                content: event.delta
              }
            ];
        });
        break;
      }
      case "REASONING_START":
        // 新协议：REASONING_*；旧协议：THINKING_*（下方分支）。二者都写入 thinkingBlocks。
        activeThinkingTitleRef.current = "思考过程";
        activeThinkingStepIdRef.current = null;
        beginThinkingBlock(
          event.messageId,
          activeRunIdRef.current ?? event.messageId
        );
        break;
      case "REASONING_MESSAGE_START": {
        const rawStep = normalizeThinkingStep(event.rawEvent);
        const step = rawStep
          ? { ...rawStep, detail: "", status: "active" as const }
          : {
            id: event.messageId,
            phase: "reasoning" as const,
            title: activeThinkingTitleRef.current,
            detail: "",
            status: "active" as const,
            iteration: thinking.length + 1,
            createdAt: new Date().toISOString()
          };
        activeThinkingStepIdRef.current = step.id;
        upsertThinkingStep(step);
        break;
      }
      case "REASONING_MESSAGE_CONTENT":
        appendThinkingStepDetail(event.messageId, event.delta);
        break;
      case "REASONING_MESSAGE_END": {
        const rawStep = normalizeThinkingStep(event.rawEvent);
        const stepId = rawStep?.id ?? event.messageId;
        if (stepId) completeThinkingStep(stepId, rawStep);
        if (activeThinkingStepIdRef.current === stepId) activeThinkingStepIdRef.current = null;
        break;
      }
      case "REASONING_END":
        activeThinkingStepIdRef.current = null;
        closeActiveThinkingBlock();
        break;
      case "THINKING_START": {
        const legacyBlockId = createClientId();
        activeThinkingTitleRef.current = event.title || "思考过程";
        activeThinkingStepIdRef.current = null;
        beginThinkingBlock(
          legacyBlockId,
          activeRunIdRef.current ?? legacyBlockId
        );
        break;
      }
      case "THINKING_TEXT_MESSAGE_START": {
        const rawStep = normalizeThinkingStep(event.rawEvent);
        const step = rawStep
          ? { ...rawStep, detail: "", status: "active" as const }
          : {
            id: createClientId(),
            phase: "reasoning" as const,
            title: activeThinkingTitleRef.current,
            detail: "",
            status: "active" as const,
            iteration: thinking.length + 1,
            createdAt: new Date().toISOString()
          };
        activeThinkingStepIdRef.current = step.id;
        upsertThinkingStep(step);
        break;
      }
      case "THINKING_TEXT_MESSAGE_CONTENT":
        if (activeThinkingStepIdRef.current) {
          appendThinkingStepDetail(activeThinkingStepIdRef.current, event.delta);
        }
        break;
      case "THINKING_TEXT_MESSAGE_END": {
        const rawStep = normalizeThinkingStep(event.rawEvent);
        const stepId = rawStep?.id ?? activeThinkingStepIdRef.current;
        if (stepId) completeThinkingStep(stepId, rawStep);
        activeThinkingStepIdRef.current = null;
        break;
      }
      case "THINKING_END":
        activeThinkingStepIdRef.current = null;
        closeActiveThinkingBlock();
        break;
      case "TOOL_CALL_START":
      {
        const toolTurnId = activeRunIdRef.current ?? event.toolCallId;
        const toolSequence = activitySequenceRef.current + 1;
        activitySequenceRef.current = toolSequence;
        // textOffset：工具卡片相对助手可见文本的插入位点，供 inline 时间线交错渲染。
        setTools((current) => [...current, {
          id: event.toolCallId,
          turnId: toolTurnId,
          name: event.toolCallName,
          arguments: "",
          status: "preparing",
          sequence: toolSequence,
          textOffset: assistantVisibleTextLengthRef.current
        }]);
        setStatus(`调用 ${event.toolCallName}`);
        break;
      }
      case "TOOL_CALL_ARGS":
        updateTool(event.toolCallId, (tool) => ({
          ...tool, arguments: tool.arguments + event.delta, status: "running"
        }));
        break;
      case "TOOL_CALL_END":
        updateTool(event.toolCallId, (tool) => ({ ...tool, status: "waiting" }));
        break;
      case "TOOL_CALL_RESULT":
        updateTool(event.toolCallId, (tool) => ({
          ...tool,
          result: event.content,
          status: toolResultFailed(event.content) ? "error" : "complete"
        }));
        break;
      case "CUSTOM":
        // 扩展通道：status/trace 文案，以及人工审批请求/结果（非标准 AG-UI 字段）。
        if (event.name === "status") {
          setStatus(String(event.value.message ?? appConfig.status.processing));
        }
        if (event.name === "trace") {
          const entry = String(event.value.entry ?? "");
          if (entry) setTrace((current) => [...current, entry]);
        }
        if (event.name === "approval_request") {
          const value = event.value;
          const id = String(value.id ?? "");
          const runId = String(value.runId ?? activeRunIdRef.current ?? "");
          if (id && runId) {
            const sequence = activitySequenceRef.current + 1;
            activitySequenceRef.current = sequence;
            setApprovals((current) => current.some((approval) => approval.id === id)
              ? current
              : [...current, {
                id,
                threadId: String(value.threadId ?? sessionId ?? ""),
                runId,
                agentKind: String(value.agentKind ?? "k_agent"),
                category: String(value.category ?? "tool"),
                title: String(value.title ?? "需要你的确认"),
                message: String(value.message ?? "请确认是否继续。"),
                detail: value.detail && typeof value.detail === "object"
                  ? value.detail as Record<string, unknown>
                  : {},
                status: "pending",
                sequence
              }]);
            setStatus("等待人工审批");
          }
        }
        if (event.name === "approval_resolved") {
          const id = String(event.value.id ?? "");
          const action = String(event.value.action ?? "cancel");
          setApprovals((current) => current.map((approval) => approval.id === id
            ? {
              ...approval,
              status: action === "approve" ? "approved" : action === "deny" ? "denied" : "cancelled"
            }
            : approval));
        }
        break;
      case "RUN_FINISHED":
        activeRunIdRef.current = null;
        setActiveRunId(null);
        pendingAssistantIdRef.current = null;
        setStatus(appConfig.status.complete);
        break;
      case "RUN_ERROR": {
        const pendingAssistantId = pendingAssistantIdRef.current;
        const failedRunId = activeRunIdRef.current;
        const errorText = (event.message || "").trim() || "服务未返回错误详情，请查看后端日志";
        if (failedRunId) failRunActivities(failedRunId);
        pendingAssistantIdRef.current = null;
        activeRunIdRef.current = null;
        setActiveRunId(null);
        setStatus(appConfig.status.failed);
        // 历史错误气泡已在 mergeHistoricalMessages 按原 run 位置插入；重放时再 append 会错序。
        if (replayingHistoryRef.current) break;
        setMessages((current) =>
          pendingAssistantId && current.some((message) => message.id === pendingAssistantId)
            ? current.map((message) =>
              message.id === pendingAssistantId
                ? { ...message, content: `${appConfig.text.requestErrorPrefix}${errorText}` }
                : message
            )
            : [
              ...current,
              {
                ...createMessage("assistant", `${appConfig.text.requestErrorPrefix}${errorText}`),
                meta: failedRunId ? { runId: failedRunId } : undefined
              }
            ]
        );
        break;
      }
    }
  }

  function failRunActivities(runId: string) {
    setTools((current) => current.map((tool) =>
      tool.turnId === runId && tool.status !== "complete"
        ? { ...tool, status: "error" }
        : tool
    ));
    setTextBlocks((current) => current.map((text) =>
      text.turnId === runId && text.status === "streaming"
        ? { ...text, status: "complete" }
        : text
    ));
    setThinking((current) => current.map((step) =>
      step.status === "active" ? { ...step, status: "error" } : step
    ));
    setThinkingBlocks((current) => current.map((block) =>
      block.turnId === runId
        ? {
          ...block,
          closed: true,
          steps: block.steps.map((step) =>
            step.status === "active" ? { ...step, status: "error" } : step
          )
        }
        : block
    ));
    setApprovals((current) => current.map((approval) =>
      approval.runId === runId && (approval.status === "pending" || approval.status === "submitting")
        ? { ...approval, status: "cancelled" }
        : approval
    ));
    activeThinkingBlockIdRef.current = null;
    activeThinkingStepIdRef.current = null;
    activeTextActivityIdRef.current = null;
    activeTextMessageIdRef.current = null;
  }

  function updateTool(id: string, transform: (tool: ToolActivity) => ToolActivity) {
    setTools((current) => current.map((tool) => tool.id === id ? transform(tool) : tool));
  }

  async function submitApproval(
    approval: ApprovalActivity,
    action: "approve" | "deny" | "cancel",
    remember = false
  ) {
    setApprovals((current) => current.map((item) => item.id === approval.id
      ? { ...item, status: "submitting", error: undefined }
      : item));
    try {
      await resolveApproval(approval.id, {
        threadId: approval.threadId,
        runId: approval.runId,
        action,
        remember
      });
      setApprovals((current) => current.map((item) => item.id === approval.id
        ? {
          ...item,
          status: action === "approve" ? "approved" : action === "deny" ? "denied" : "cancelled"
        }
        : item));
      setStatus(action === "approve" ? appConfig.status.processing : "已拒绝工具调用");
    } catch (error) {
      setApprovals((current) => current.map((item) => item.id === approval.id
        ? {
          ...item,
          status: "error",
          error: error instanceof Error ? error.message : "审批提交失败"
        }
        : item));
    }
  }

  function beginThinkingBlock(blockId: string, turnId: string) {
    activeThinkingBlockIdRef.current = blockId;
    const blockSequence = activitySequenceRef.current + 1;
    activitySequenceRef.current = blockSequence;
    setThinkingBlocks((current) =>
      current.some((block) => block.id === blockId)
        ? current
        : [
          ...current,
          {
            id: blockId,
            turnId,
            steps: [],
            closed: false,
            sequence: blockSequence
          }
        ]
    );
  }

  function upsertThinkingStep(
    step: ThinkingActivity,
    turnId = activeRunIdRef.current ?? step.id
  ) {
    let blockId = activeThinkingBlockIdRef.current;
    let blockSequence = activitySequenceRef.current;
    if (!blockId) {
      blockId = createClientId();
      activeThinkingBlockIdRef.current = blockId;
      blockSequence += 1;
      activitySequenceRef.current = blockSequence;
    }
    setThinking((current) => {
      const exists = current.some((item) => item.id === step.id);
      return exists
        ? current.map((item) => item.id === step.id ? step : item)
        : [...current, step];
    });
    setThinkingBlocks((current) => {
      const existingBlock = current.find((block) => block.steps.some((item) => item.id === step.id));
      if (existingBlock) {
        return current.map((block) =>
          block.id === existingBlock.id
            ? {
              ...block,
              steps: block.steps.map((item) => item.id === step.id ? step : item)
            }
            : block
        );
      }
      const targetBlocks = current.some((block) => block.id === blockId)
        ? current
        : [
          ...current,
          {
            id: blockId,
            turnId,
            steps: [],
            closed: false,
            sequence: blockSequence
          }
        ];
      return targetBlocks.map((block) => {
        if (block.id !== blockId) return block;
        const exists = block.steps.some((item) => item.id === step.id);
        return {
          ...block,
          steps: exists
            ? block.steps.map((item) => item.id === step.id ? step : item)
            : [...block.steps, step],
        };
      });
    });
  }

  function appendThinkingStepDetail(id: string, delta: string) {
    setThinking((current) => current.map((step) =>
      step.id === id ? { ...step, detail: step.detail + delta } : step
    ));
    setThinkingBlocks((current) => current.map((block) => ({
      ...block,
      steps: block.steps.map((step) =>
        step.id === id ? { ...step, detail: step.detail + delta } : step
      )
    })));
  }

  function completeThinkingStep(id: string, finalStep: ThinkingActivity | null) {
    const complete = (step: ThinkingActivity) =>
      step.id === id
        ? { ...(finalStep ?? step), id, status: "complete" as const }
        : step;
    setThinking((current) => current.map(complete));
    setThinkingBlocks((current) => current.map((block) => ({
      ...block,
      steps: block.steps.map(complete)
    })));
  }

  function closeActiveThinkingBlock() {
    const activeBlockId = activeThinkingBlockIdRef.current;
    if (!activeBlockId) return;
    activeThinkingBlockIdRef.current = null;
    setThinkingBlocks((current) => current.map((block) =>
      block.id === activeBlockId
        ? { ...block, closed: true }
        : block
    ));
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      const nativeEvent = event.nativeEvent as KeyboardEvent<HTMLTextAreaElement>["nativeEvent"] & {
        keyCode?: number;
      };
      if (isComposerComposingRef.current || nativeEvent.isComposing || nativeEvent.keyCode === 229) {
        return;
      }
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  async function addImages(files: FileList | null) {
    if (!files) return;
    const selectedModel = models.find((model) => model.id === modelId);
    if (!selectedModel?.multimodal) {
      setStatus("当前模型不支持图片输入");
      return;
    }
    const images = Array.from(files).filter((file) => file.type.startsWith("image/")).slice(0, 4);
    const encoded = await Promise.all(images.map((file) => new Promise<{ name: string; dataUrl: string; type: string }>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve({ name: file.name, dataUrl: String(reader.result), type: file.type });
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    })));
    setAttachments((current) => [...current, ...encoded].slice(0, 4));
  }

  function toggleVoiceInput() {
    if (listening) {
      speechRef.current?.stop();
      return;
    }
    const SpeechRecognition = getSpeechRecognitionConstructor();
    if (!SpeechRecognition) {
      setStatus("当前浏览器不支持语音输入");
      return;
    }

    // Plain voice input remains a one-shot dictation feature: it only updates
    // the composer and never submits or enables assistant speech automatically.
    const recognition = new SpeechRecognition();
    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.continuous = false;
    const originalInput = composerInputRef.current;
    recognition.onstart = () => setListening(true);
    recognition.onend = () => {
      setListening(false);
      if (speechRef.current === recognition) speechRef.current = null;
    };
    recognition.onerror = () => {
      setListening(false);
      if (speechRef.current === recognition) speechRef.current = null;
      setStatus("语音输入未能启动");
    };
    recognition.onresult = (event) => {
      let transcript = "";
      for (let index = 0; index < event.results.length; index += 1) {
        transcript += event.results[index][0]?.transcript ?? "";
      }
      updateComposerInput(`${originalInput}${originalInput && transcript ? " " : ""}${transcript}`);
    };
    speechRef.current = recognition;
    try {
      recognition.start();
    } catch {
      speechRef.current = null;
      setListening(false);
      setStatus("语音输入未能启动");
    }
  }

  function startVoiceConversation() {
    const SpeechRecognition = getSpeechRecognitionConstructor();
    if (!SpeechRecognition) {
      setStatus("当前浏览器不支持语音输入");
      setVoiceConversation((current) => ({
        ...current,
        supported: false,
        active: false,
        phase: "error",
        message: "当前浏览器不支持语音输入"
      }));
      return;
    }
    if (!modelId || sessionLoading) {
      setStatus(!modelId ? "请先选择可用模型" : "会话仍在加载，请稍后再试");
      return;
    }

    voiceConversationActiveRef.current = true;
    voiceInputMutedRef.current = false;
    voiceRunFinishedRef.current = !loading;
    setVoiceConversation({
      supported: true,
      active: true,
      inputMuted: false,
      phase: loading ? "processing" : "listening",
      message: loading ? "正在等待助手回复" : "正在聆听"
    });

    // A voice conversation owns both browser speech directions. Enable TTS in
    // the same tick so an immediately auto-submitted turn cannot outrun React.
    if (voicePlayback.supported) {
      voiceAutoEnabledRef.current = true;
      setVoicePlayback((current) => ({
        ...current,
        autoEnabled: true,
        status: "idle",
        message: "语音会话已开启"
      }));
    }
    // A dictation recognition may still be shutting down when the user moves
    // directly into conversation mode; release it before starting the owner.
    if (speechRef.current) {
      speechRef.current.stop();
      speechRef.current = null;
      setListening(false);
    }
    if (!loading) window.setTimeout(() => startVoiceRecognition(), 180);
  }

  function stopVoiceConversation(message = "语音会话已结束") {
    if (!voiceConversationActiveRef.current) {
      stopVoicePlayback(message);
      return;
    }
    voiceConversationActiveRef.current = false;
    voiceInputMutedRef.current = false;
    voiceRecognitionStopReasonRef.current = "exit";
    speechRef.current?.stop();
    speechRef.current = null;
    setListening(false);
    cancelVoiceEngine();
    setVoiceConversation((current) => ({
      ...current,
      active: false,
      inputMuted: false,
      phase: "inactive",
      message: ""
    }));
    setVoicePlayback((current) => ({
      ...current,
      autoEnabled: false,
      status: current.supported ? "idle" : "unsupported",
      message: current.supported ? message : current.message,
      messageId: undefined
    }));
    voiceAutoEnabledRef.current = false;
  }

  function toggleVoiceConversationMicrophone() {
    if (voiceInputMutedRef.current) {
      voiceInputMutedRef.current = false;
      setVoiceConversation((current) => ({ ...current, inputMuted: false, phase: "listening", message: "正在聆听" }));
      startVoiceRecognition();
      return;
    }
    voiceInputMutedRef.current = true;
    voiceRecognitionStopReasonRef.current = "mute";
    speechRef.current?.stop();
    setVoiceConversation((current) => ({ ...current, inputMuted: true, phase: "inactive", message: "麦克风已暂停" }));
  }

  function interruptActiveVoiceTurnForBargeIn(transcript: string) {
    if (!voiceSpeakingRef.current || voiceBargeInActiveRef.current) return;
    voiceBargeInActiveRef.current = true;
    const activeSessionId = activeSessionIdRef.current;
    if (activeSessionId) {
      activeRunsRef.current.get(activeSessionId)?.controller.abort();
    }
    cancelVoiceEngine();
    voiceRunFinishedRef.current = true;
    setLoading(false);
    setStatus("已打断助手，正在聆听");
    setVoicePlayback((current) => ({
      ...current,
      status: current.supported ? "idle" : "unsupported",
      message: "已被用户打断",
      messageId: undefined
    }));
    setVoiceConversation((current) => ({
      ...current,
      phase: "listening",
      message: transcript || "已打断，继续说完后将自动发送"
    }));
  }

  function startVoiceRecognition(mode: "turn" | "barge-in" = "turn") {
    if (
      !voiceConversationActiveRef.current
      || voiceInputMutedRef.current
      || (mode === "turn" && !voiceRunFinishedRef.current)
      || speechRef.current
    ) return;
    const SpeechRecognition = getSpeechRecognitionConstructor();
    if (!SpeechRecognition) {
      setVoiceConversation((current) => ({ ...current, phase: "error", message: "浏览器语音识别不可用" }));
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.continuous = false;
    const originalInput = composerInputRef.current;
    let transcript = "";
    let recognitionError: string | null = null;
    let bargeInTriggered = false;
    voiceRecognitionStopReasonRef.current = null;
    recognition.onstart = () => {
      setListening(true);
      if (mode === "barge-in" && voiceSpeakingRef.current) {
        setVoiceConversation((current) => ({ ...current, phase: "speaking", message: "助手正在说话，你可以直接说话打断" }));
      } else {
        setVoiceConversation((current) => ({ ...current, phase: "listening", message: "正在聆听，说完后将自动发送" }));
      }
    };
    recognition.onend = () => {
      setListening(false);
      if (speechRef.current === recognition) speechRef.current = null;
      const stopReason = voiceRecognitionStopReasonRef.current;
      voiceRecognitionStopReasonRef.current = null;
      if (
        !voiceConversationActiveRef.current
        || stopReason === "exit"
        || stopReason === "mute"
        || stopReason === "submit"
      ) return;

      if (mode === "barge-in" && !bargeInTriggered) {
        // A barge-in listener that heard only synthesized speech must discard
        // that transcript instead of submitting the assistant's own words.
        if (!recognitionError || recognitionError === "no-speech" || recognitionError === "aborted") {
          window.setTimeout(
            () => startVoiceRecognition(voiceSpeakingRef.current || !voiceRunFinishedRef.current ? "barge-in" : "turn"),
            250
          );
        }
        return;
      }

      const prompt = `${originalInput}${originalInput && transcript ? " " : ""}${transcript}`.trim();
      if (prompt && !recognitionError) {
        // requestSubmit invokes the current React handler after setInput has
        // committed, while the ref carries the exact final transcript.
        voiceAutoSubmitPromptRef.current = prompt;
        updateComposerInput(prompt);
        setVoiceConversation((current) => ({ ...current, phase: "processing", message: "已识别，正在发送" }));
        composerTextareaRef.current?.form?.requestSubmit();
        return;
      }
      if (!recognitionError || recognitionError === "no-speech" || recognitionError === "aborted") {
        window.setTimeout(
          () => startVoiceRecognition(voiceSpeakingRef.current || !voiceRunFinishedRef.current ? "barge-in" : "turn"),
          250
        );
      }
    };
    recognition.onerror = (event) => {
      recognitionError = event.error ?? "unknown";
      setListening(false);
      if (recognitionError === "no-speech" || recognitionError === "aborted") return;
      setVoiceConversation((current) => ({ ...current, phase: "error", message: "语音识别失败，请检查麦克风权限" }));
      setStatus("语音输入未能启动");
    };
    recognition.onresult = (event) => {
      transcript = "";
      for (let index = 0; index < event.results.length; index += 1) {
        transcript += event.results[index][0]?.transcript ?? "";
      }
      if (mode === "barge-in" && !bargeInTriggered) {
        const shouldInterrupt = shouldInterruptForTranscript(
          transcript,
          voiceSpeakingRef.current ? voiceEchoReferenceTextRef.current : ""
        );
        if (!shouldInterrupt) return;
        bargeInTriggered = true;
        if (voiceSpeakingRef.current) interruptActiveVoiceTurnForBargeIn(transcript);
      }
      updateComposerInput(`${originalInput}${originalInput && transcript ? " " : ""}${transcript}`);
      setVoiceConversation((current) => ({ ...current, phase: "listening", message: transcript || "正在聆听" }));
    };
    speechRef.current = recognition;
    try {
      recognition.start();
    } catch {
      speechRef.current = null;
      setVoiceConversation((current) => ({ ...current, phase: "error", message: "语音识别启动失败" }));
    }
  }

  if (view === "config") {
    return (
      <>
        <ConfigCenter onBack={() => {
          // ConfigCenter persists device-local voices independently of server catalogs.
          setVoiceConfig(readVoiceConfig());
          setView("chat");
          void refreshOptions();
        }} />
        <DesktopPet enabled={desktopPetEnabled} onEnabledChange={setDesktopPetEnabled} />
      </>
    );
  }

  if (view === "team") {
    return (
      <>
        <TeamWorkbench
          onBack={() => setView("chat")}
          onOpenConfig={() => setView("config")}
          health={health}
          onRefreshHealth={() => void refreshHealth()}
          sidebarWidth={sidebarWidth}
          sidebarOpen={sidebarOpen}
          sidebarCollapsed={sidebarCollapsed}
          stageOpen={teamStageOpen}
          stageWidth={inspectorWidth}
          desktopPetEnabled={desktopPetEnabled}
          onToggleDesktopPet={() => setDesktopPetEnabled((current) => !current)}
          onCloseSidebar={() => setSidebarOpen(false)}
          onToggleSidebar={() => {
            if (window.matchMedia("(max-width: 860px)").matches) {
              setSidebarCollapsed(false);
              setSidebarOpen(true);
              return;
            }
            setSidebarCollapsed((current) => !current);
            setSidebarOpen(false);
          }}
          onToggleStage={() => setTeamStageOpen((value) => !value)}
          onOpenStage={() => setTeamStageOpen(true)}
          onBeginSidebarResize={(event) => beginResize("sidebar", event)}
          onResizeSidebarWithKeyboard={(event) => resizeWithKeyboard("sidebar", event)}
          onBeginStageResize={(event) => beginResize("inspector", event)}
          onResizeStageWithKeyboard={(event) => resizeWithKeyboard("inspector", event)}
        />
        <DesktopPet enabled={desktopPetEnabled} onEnabledChange={setDesktopPetEnabled} />
      </>
    );
  }

  return (
    <>
    <div
      className={`shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${inspectorOpen && view === "chat" ? "" : "inspector-hidden"}`}
      style={{
        "--sidebar-width": `${sidebarWidth}px`,
        "--inspector-width": `${inspectorWidth}px`
      } as React.CSSProperties}
    >
      <button
        className={`scrim ${sidebarOpen ? "visible" : ""}`}
        type="button"
        aria-label="关闭会话侧栏"
        onClick={() => setSidebarOpen(false)}
      />

      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="brand">
          <img className="brand-symbol" src="/brand-k.png" alt="" width={32} height={32} />
          <div><strong>K Agent</strong><small>本地执行台</small></div>
        </div>
        <nav className="workspace-switch" aria-label="工作模式">
          <button className="active" type="button" aria-current="page" onClick={() => setView("chat")}>
            <span>◉</span><strong>Work</strong>
          </button>
          <button type="button" onClick={() => setView("team")}>
            <span>⌘</span><strong>Agent Team</strong>
          </button>
        </nav>
        <button className="new-session" type="button" onClick={() => { setView("chat"); startNewSession(); }}>
          <span>＋</span> 新建会话 <kbd>⌘ N</kbd>
        </button>
        <button className={`new-session scheduled-session-entry ${view === "scheduled" ? "active" : ""}`} type="button" onClick={() => setView("scheduled")}>
          <span>◷</span> 定时任务
        </button>
        <label className="session-search">
          <span>⌕</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索会话"
            aria-label="搜索会话"
          />
        </label>
        <div className="section-label"><span>最近会话</span><b>{sessions.length}</b></div>
        <nav className="session-list" aria-label="会话列表">
          {filteredSessions.length === 0 && (
            <p className="empty-note">{query ? "没有匹配的会话" : "你的对话会显示在这里"}</p>
          )}
          {filteredSessions.map((session) => (
            <button
              className={`session-item ${session.id === sessionId ? "active" : ""} ${runningSessionIds.has(session.id) ? "running" : ""}`}
              key={session.id}
              type="button"
              onClick={() => { setView("chat"); void openSession(session.id); }}
            >
              {runningSessionIds.has(session.id)
                ? (
                  <span
                    className="session-run-indicator"
                    role="status"
                    aria-label="正在流式执行"
                    title="正在流式执行"
                  />
                )
                : <span className="session-glyph">◫</span>}
              <span className="session-copy">
                <strong>{session.title}</strong>
                <small>
                  {runningSessionIds.has(session.id)
                    ? "正在流式执行"
                    : `${formatRelativeTime(session.updatedAt)} · ${session.messageCount} 条`}
                </small>
              </span>
            </button>
          ))}
        </nav>
        <button
          className="connection"
          type="button"
          onClick={() => void refreshHealth()}
          title={
            health?.bashSandbox?.userSummary
            || health?.bashSandbox?.reason
            || (health?.ok ? "点击重新检查服务与沙箱状态" : "点击重新检查")
          }
        >
          <span className={`connection-dot ${health?.ok ? "online" : ""}`} />
          <span>
            <strong>{health?.ok ? "服务运行正常" : "后端未连接"}</strong>
            <small>
              {health
                ? `${health.model} · ${health.localToolCount + health.mcpToolCount} 个工具 · ${
                    health.bashSandbox?.available
                      ? "沙箱就绪"
                      : health.bashSandbox?.mode === "off"
                        ? "沙箱关闭"
                        : health.bashSandbox?.needsInstall
                          ? "沙箱未安装（悬停看安装说明）"
                          : health.bashSandbox
                            ? "沙箱不可用（悬停看说明）"
                            : "沙箱未知"
                  }`
                : "点击重新检查"}
            </small>
          </span>
          <i>↻</i>
        </button>
        <button className="settings-link" type="button" onClick={() => setView("config")}>
          <span>⚙</span><strong>配置中心</strong><small>MCP · Skills · 模型</small><i>→</i>
        </button>
      </aside>
      <div
        className="resize-handle resize-handle-left"
        role="separator"
        tabIndex={0}
        aria-label="调整会话侧栏宽度"
        aria-orientation="vertical"
        onPointerDown={(event) => beginResize("sidebar", event)}
        onKeyDown={(event) => resizeWithKeyboard("sidebar", event)}
      />

      <main className={`conversation ${voiceConversation.active ? "voice-conversation-mode" : ""} ${view === "scheduled" ? "scheduled-conversation" : ""}`}>
        {view === "scheduled" ? (
          <ScheduledTasksView
            models={models}
            agents={agents}
            mcpServers={mcpServers}
            skills={skills}
          />
        ) : (
        <>
        <header className="topbar">
          <button
            className="topbar-icon-button sidebar-toggle-button"
            type="button"
            onClick={() => {
              if (window.matchMedia("(max-width: 860px)").matches) {
                setSidebarCollapsed(false);
                setSidebarOpen(true);
                return;
              }
              setSidebarCollapsed((current) => !current);
              setSidebarOpen(false);
            }}
            aria-label={sidebarCollapsed ? "展开会话侧栏" : "折叠会话侧栏"}
          >
            <SidebarIcon side="left" />
          </button>
          <div className="thread-heading">
            <h1>{activeSession?.title ?? "今天想一起完成什么？"}</h1>
          </div>
          <div className="topbar-actions">
            <span className={`status-pill ${loading ? "running" : ""}`}>
              <i />{status}
            </span>
            <button
              className={`topbar-icon-button desktop-pet-toggle ${desktopPetEnabled ? "active" : ""}`}
              type="button"
              aria-pressed={desktopPetEnabled}
              aria-label={desktopPetEnabled ? "关闭桌宠" : "开启桌宠"}
              title={desktopPetEnabled ? "桌宠已开启，点击关闭" : "桌宠已关闭，点击开启"}
              onClick={() => setDesktopPetEnabled((current) => !current)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 8.2c-4 0-7.1 2.8-7.1 6.5 0 3.1 2.5 5.2 7.1 5.2s7.1-2.1 7.1-5.2c0-3.7-3.1-6.5-7.1-6.5Z" />
                <path d="m6.4 11.1-.8-4.2 4 2.1M17.6 11.1l.8-4.2-4 2.1M12 8V4.8M12 5c.1-1.7 1.4-2.8 3.2-2.8-.1 1.7-1.4 2.8-3.2 2.8ZM11.9 5C11.8 3.6 10.7 2.7 9.3 2.7c.1 1.4 1.2 2.3 2.6 2.3Z" />
                <path d="M9.2 14.3h.1M14.7 14.3h.1M10.8 16.5c.8.6 1.6.6 2.4 0" />
              </svg>
            </button>
            <button
              className="topbar-icon-button theme-button"
              type="button"
              aria-label="切换页面主题"
              title={`当前主题：${colorThemes.find((theme) => theme.id === colorTheme)?.name ?? "白色"}，点击切换`}
              onClick={() => {
                const currentIndex = colorThemes.findIndex((theme) => theme.id === colorTheme);
                setColorTheme(colorThemes[(currentIndex + 1) % colorThemes.length].id);
              }}
            >
              <i />
            </button>
            <div className="font-size-control">
              <button
                className="topbar-icon-button font-size-button"
                type="button"
                aria-label="调节字体大小"
                title={`字体偏移：${fontSizeDelta >= 0 ? "+" : ""}${fontSizeDelta}px，点击调节`}
                onClick={() => setFontSizePopoverOpen((v) => !v)}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <text x="2" y="20" fontSize="16" fontWeight="700" fill="currentColor" stroke="none" fontFamily="sans-serif">A</text>
                  <text x="14" y="14" fontSize="10" fontWeight="600" fill="currentColor" stroke="none" fontFamily="sans-serif">A</text>
                </svg>
              </button>
              {fontSizePopoverOpen && (
                <div className="font-size-popover">
                  <div className="font-size-header">
                    <span className="font-size-title">字体大小</span>
                    <span className="font-size-value">{fontSizeDelta >= 0 ? "+" : ""}{fontSizeDelta}px</span>
                  </div>
                  <input
                    type="range"
                    min={-2}
                    max={6}
                    step={1}
                    value={fontSizeDelta}
                    onChange={(e) => setFontSizeDelta(Number(e.target.value))}
                    className="font-size-slider"
                  />
                  <div className="font-size-range-labels">
                    <span>-2</span>
                    <span>0</span>
                    <span>+3</span>
                    <span>+6</span>
                  </div>
                  {fontSizeDelta !== 0 && (
                    <button
                      className="font-size-reset"
                      type="button"
                      onClick={() => setFontSizeDelta(0)}
                    >
                      重置为默认
                    </button>
                  )}
                </div>
              )}
            </div>
            <button className="topbar-icon-button" type="button" onClick={() => setInspectorOpen((value) => !value)} aria-label="切换右侧面板">
              <SidebarIcon side="right" />
            </button>
          </div>
        </header>

        <section
          ref={messageStreamRef}
          className="message-stream"
          aria-live="polite"
          onScroll={handleMessageStreamScroll}
        >
          {displayMessages.length === 0 && (
            <div className="welcome">
              <div className="welcome-atmosphere" aria-hidden="true" />
              <p className="welcome-brand">K Agent</p>
              <h2>描述目标，开始执行。</h2>
              <p className="welcome-copy">拆解任务、调用工具、保留上下文——都在这张执行台上完成。</p>
              <div className="welcome-dispatch">
                <span>下达一条任务</span>
                <div className="suggestions">
                  {suggestions.map((suggestion) => (
                    <button key={suggestion} type="button" onClick={() => updateComposerInput(suggestion)}>
                      {suggestion}<i>↗</i>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {displayMessages.length > 0 && displayMessages.map((message) => {
            const turnId = message.role === "assistant" ? message.meta?.runId : undefined;
            const blocks = turnId
              ? thinkingBlocks.filter((block) => block.turnId === turnId)
              : [];
            const messageTools = turnId
              ? tools.filter((tool) => tool.turnId === turnId)
              : [];
            const messageTexts = turnId
              ? textBlocks.filter((text) => text.turnId === turnId)
              : [];
            const messageApprovals = turnId
              ? approvals.filter((approval) => approval.runId === turnId)
              : [];
            const timeline = inlineTimeline(messageTexts, blocks, messageTools, messageApprovals);
            const hasTimelineText = messageTexts.length > 0;
            const isActiveAssistant = message.role === "assistant"
              && loading
              && (!turnId || turnId === activeRunId);

            return (
              <MessageRenderBoundary key={message.id} messageId={message.id}>
                <article className={`message-row ${message.role}`}>
                  <div className="avatar">{message.role === "user" ? "你" : "K"}</div>
                  <div className="message-body">
                    {message.role === "assistant" && timeline.map((activity) =>
                      activity.type === "thinking"
                        ? <InlineThinking block={activity.block} key={`thinking-${activity.block.id}`} />
                        : activity.type === "tool"
                          ? <InlineToolActivity tool={activity.tool} key={`tool-${activity.tool.id}`} />
                          : activity.type === "approval"
                            ? (
                              <ApprovalCard
                                approval={activity.approval}
                                key={`approval-${activity.approval.id}`}
                                onDecision={submitApproval}
                              />
                            )
                          : (
                            <div className="assistant-output" key={`text-${activity.text.id}`}>
                              <MarkdownContent content={activity.text.content} />
                            </div>
                          )
                    )}
                    {message.role === "assistant"
                      ? !hasTimelineText && (
                        <div className="assistant-output">
                          {message.content
                            ? <MarkdownContent content={message.content} />
                            : <Typing />}
                        </div>
                      )
                      : (
                        <div className="bubble">
                          {message.content ? <MarkdownContent content={message.content} /> : <Typing />}
                        </div>
                      )}
                    {!isActiveAssistant && (
                      <footer className="message-meta">
                        <time>{formatTime(message.createdAt)}</time>
                        {message.content && (
                          <>
                          {message.role === "assistant" && (
                            <button
                              className={voicePlayback.messageId === message.id && voicePlayback.status === "speaking" ? "voice-feedback speaking" : ""}
                              type="button"
                              aria-label={voicePlayback.messageId === message.id && voicePlayback.status === "speaking"
                                ? "停止朗读这条消息"
                                : "朗读这条消息"}
                              title={voicePlayback.messageId === message.id && voicePlayback.status === "speaking"
                                ? "停止朗读"
                                : "朗读消息"}
                              onClick={() => {
                                if (voicePlayback.messageId === message.id && voicePlayback.status === "speaking") {
                                  stopVoicePlayback("朗读已停止");
                                } else {
                                  speakMessage(message.id, message.content);
                                }
                              }}
                            >
                              <span aria-hidden="true">{voicePlayback.messageId === message.id && voicePlayback.status === "speaking" ? "■" : "◉"}</span>
                              {voicePlayback.messageId === message.id && voicePlayback.status === "speaking" && (
                                <b role="status" aria-live="polite">朗读中</b>
                              )}
                            </button>
                          )}
                          <button
                            className={copyFeedback?.messageId === message.id ? `copy-feedback ${copyFeedback.status}` : ""}
                            type="button"
                            aria-label={copyFeedback?.messageId === message.id
                              ? copyFeedback.status === "success" ? "消息已复制" : "消息复制失败"
                              : "复制消息"}
                            title={copyFeedback?.messageId === message.id
                              ? copyFeedback.status === "success" ? "已复制" : "复制失败"
                              : "复制消息"}
                            onClick={() => void copyMessage(message.id, message.content)}
                          >
                            <span aria-hidden="true">{copyFeedback?.messageId === message.id && copyFeedback.status === "success" ? "✓" : "⧉"}</span>
                            {copyFeedback?.messageId === message.id && (
                              <b role="status" aria-live="polite">
                                {copyFeedback.status === "success" ? "已复制" : "复制失败"}
                              </b>
                            )}
                          </button>
                          </>
                        )}
                      </footer>
                    )}
                  </div>
                </article>
              </MessageRenderBoundary>
            );
          })}
          <div ref={streamEndRef} />
        </section>

        {voiceConversation.active && (
          <div className={`voice-conversation-float ${voiceConversation.phase}`} role="status" aria-live="polite">
            <button
              className="voice-orb"
              type="button"
              onClick={toggleVoiceConversationMicrophone}
              aria-label={voiceConversation.inputMuted ? "恢复语音对话麦克风" : "暂停语音对话麦克风"}
              title={voiceConversation.inputMuted ? "恢复麦克风" : "暂停麦克风"}
            >
              <i /><i /><i /><i />
            </button>
            <div className="voice-conversation-copy">
              <strong>{voiceConversation.phase === "listening"
                ? "正在聆听"
                : voiceConversation.phase === "processing"
                  ? "正在思考"
                  : voiceConversation.phase === "speaking"
                    ? "正在回答"
                    : voiceConversation.phase === "error"
                      ? "语音会话遇到问题"
                      : "语音会话已暂停"}</strong>
              <small>{voiceConversation.message}</small>
            </div>
          </div>
        )}
        <form className="composer" onSubmit={handleSubmit}>
          {attachments.length > 0 && <div className="attachment-list">{attachments.map((attachment, index) => <span key={`${attachment.name}-${index}`}>▧ {attachment.name}<button type="button" onClick={() => setAttachments(attachments.filter((_, i) => i !== index))}>×</button></span>)}</div>}
          <textarea
            ref={composerTextareaRef}
            aria-label="给 Agent 发送消息"
            value={input}
            onChange={(event) => updateComposerInput(event.target.value)}
            onKeyDown={handleComposerKeyDown}
            onCompositionStart={() => {
              isComposerComposingRef.current = true;
            }}
            onCompositionEnd={() => {
              isComposerComposingRef.current = false;
            }}
            placeholder="写下目标或约束，开始这一轮执行…"
            rows={2}
          />
          <div className="composer-footer">
            <div className="composer-toolbar composer-toolbar-left">
              <ComposerAddMenu
                imageEnabled={agentKind === "k_agent" && Boolean(models.find((model) => model.id === modelId)?.multimodal)}
                onImages={(files) => { void addImages(files); }}
                mcpItems={mcpServers.map((server) => ({ id: server.id, name: server.name || server.id, description: server.description }))}
                selectedMcp={selectedMcp}
                onMcpChange={changeSelectedMcp}
                skillItems={skills.map((skill) => ({ id: skill.id, name: skill.name, description: skill.description }))}
                selectedSkills={selectedSkills}
                onSkillsChange={changeSelectedSkills}
              />
            </div>
            <div className="composer-toolbar composer-toolbar-right">
              <RuntimeSettingsPicker
                agentKind={agentKind}
                agents={agents}
                cliSessionMode={cliSessionMode}
                modelId={modelId}
                kAgentModels={models}
                reasoningEffort={reasoningEffort}
                onRuntimeChange={(nextAgent, nextModel) => {
                  setAgentKind(nextAgent);
                  localStorage.setItem(AGENT_KIND_STORAGE_KEY, nextAgent);
                  if (nextAgent !== "k_agent" || !models.find((model) => model.id === nextModel)?.multimodal) setAttachments([]);
                  setModelId(nextModel);
                  if (nextModel) writeModelForAgent(nextAgent, nextModel);
                  setReasoningEffort("none");
                }}
                onCliSessionModeChange={(value) => {
                  setCliSessionMode(value);
                  localStorage.setItem(CLI_SESSION_MODE_STORAGE_KEY, value);
                }}
                onReasoningChange={setReasoningEffort}
              />
              <button className={`voice-button ${listening && !voiceConversation.active ? "listening" : ""}`} type="button" onClick={toggleVoiceInput} disabled={voiceConversation.active} aria-label={voiceConversation.active ? "语音对话中不可单独听写" : listening ? "停止语音输入" : "语音输入"} title={voiceConversation.active ? "语音对话中不可单独听写" : listening ? "停止语音输入" : "语音输入"}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z"/><path d="M18 11v1a6 6 0 0 1-12 0v-1M12 18v3M9 21h6"/></svg>
              </button>
              <button className={`voice-button voice-conversation-start-button ${voiceConversation.active ? "enabled" : ""}`} type="button" onClick={voiceConversation.active ? () => stopVoiceConversation() : startVoiceConversation} aria-label={voiceConversation.active ? "结束语音对话" : "开启语音对话"} title={voiceConversation.active ? "结束语音对话" : "开启语音对话"}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h2M8 8v8M12 5v14M16 8v8M20 12h-2"/></svg>
              </button>
              {loading ? (
                <button className="stop-button" type="button" onClick={stopRun} aria-label="停止生成">■</button>
              ) : (
                <button className="send-button" type="submit" disabled={(!input.trim() && !attachments.length) || sessionLoading || !modelId} aria-label="发送">↑</button>
              )}
            </div>
          </div>
        </form>
        <p className="disclaimer">Agent 可能会犯错，关键结果请核实。</p>
        </>
        )}
      </main>

      <div
        className="resize-handle resize-handle-right"
        role="separator"
        tabIndex={0}
        aria-label="调整右侧面板宽度"
        aria-orientation="vertical"
        onPointerDown={(event) => beginResize("inspector", event)}
        onKeyDown={(event) => resizeWithKeyboard("inspector", event)}
      />
      <aside className="inspector" aria-label={rightPanelMode === "trace" ? "运行轨迹" : "工作区"}>
        <header className="inspector-header inspector-header-switch">
          <div className="inspector-header-title">
            <p className="eyebrow">{rightPanelMode === "trace" ? "RUN DETAILS" : "WORKSPACE"}</p>
            <h2>{rightPanelMode === "trace" ? "运行轨迹" : "工作区"}</h2>
          </div>
          <div className="inspector-mode-switch" role="tablist" aria-label="右侧面板模式">
            <button
              className={rightPanelMode === "trace" ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={rightPanelMode === "trace"}
              onClick={() => setRightPanelMode("trace")}
            >
              轨迹
            </button>
            <button
              className={rightPanelMode === "workspace" ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={rightPanelMode === "workspace"}
              onClick={() => setRightPanelMode("workspace")}
            >
              工作区
            </button>
          </div>
        </header>

        {rightPanelMode === "trace" ? (
          <div className="inspector-scroll">
            <section className="run-summary">
              <div>
                <span className={`run-orb ${loading ? "running" : ""}`}>◆</span>
                <strong>{loading ? "Agent 正在工作" : "等待新任务"}</strong>
              </div>
              <p>{status}</p>
              <dl>
                <div><dt>工具调用</dt><dd>{tools.length}</dd></div>
                <div><dt>轨迹事件</dt><dd>{trace.length}</dd></div>
                <div><dt>思考阶段</dt><dd>{thinking.length}</dd></div>
              </dl>
            </section>
            <InspectorSection title="思考过程" count={thinking.length}>
              {thinking.length ? (
                <div className="thinking-list">
                  {thinking.map((step) => (
                    <article className={`thinking-step ${step.status}`} key={step.id}>
                      <span className="thinking-icon">{thinkingIcon(step.phase)}</span>
                      <div>
                        <strong>{step.title}</strong>
                        <p>{step.detail}</p>
                      </div>
                      <i />
                    </article>
                  ))}
                </div>
              ) : (
                <EmptyInspector text="开始任务后会显示执行思路" />
              )}
            </InspectorSection>
            <InspectorSection title="任务计划" count={tasks.length}>
              {tasks.length ? tasks.map((task, index) => (
                <div className="task-row" key={`${task}-${index}`}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <p>{task}</p>
                </div>
              )) : (
                <EmptyInspector text="任务开始后会生成执行步骤" />
              )}
            </InspectorSection>
            <InspectorSection title="工具活动" count={tools.length}>
              {tools.length ? tools.map((tool) => (
                <details className="tool-row" key={tool.id}>
                  <summary>
                    <ToolIcon className="tool-row-icon" />
                    <strong>{tool.name}</strong>
                    <i className={tool.status} />
                  </summary>
                  {tool.arguments && <code>{tool.arguments}</code>}
                  {tool.result && <p>{tool.result}</p>}
                </details>
              )) : (
                <EmptyInspector text="暂时没有工具调用" />
              )}
            </InspectorSection>
            <InspectorSection title="执行轨迹" count={trace.length}>
              {trace.length ? (
                <ol className="timeline">
                  {trace.map((entry, index) => (
                    <li key={`${entry}-${index}`}>{entry}</li>
                  ))}
                </ol>
              ) : (
                <EmptyInspector text="运行轨迹会实时显示" />
              )}
            </InspectorSection>
          </div>
        ) : !sessionId ? (
          <ContentStage
            embedded
            items={[]}
            selectedId={null}
            onSelect={() => undefined}
            emptyTitle="先打开或创建会话"
            emptyHint="打开会话后，Agent 写入的文件会出现在这里"
          />
        ) : (
          <ContentStage
            embedded
            items={workspaceItems}
            selectedId={selectedWorkspacePath}
            onSelect={(path) => { void openWorkspaceFile(path); }}
            resolveFile={resolveWorkspaceAsset}
            emptyTitle={workspaceLoading ? "正在读取工作区…" : "工作区还没有可预览文件"}
            emptyHint={
              workspaceError
                || "Agent 在本会话写出的文档会出现在这里"
            }
          />
        )}
      </aside>
    </div>
    <DesktopPet enabled={desktopPetEnabled} onEnabledChange={setDesktopPetEnabled} />
    </>
  );
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)}KB`;
  return `${(size / (1024 * 1024)).toFixed(1)}MB`;
}

function formatWorkspacePreview(payload: {
  content: string;
  truncated: boolean;
  binary: boolean;
  size: number;
}): string {
  if (payload.binary) {
    return `（二进制文件，约 ${formatBytes(payload.size)}，暂不预览）`;
  }
  if (payload.truncated) {
    return `${payload.content}\n\n…（内容已截断）`;
  }
  return payload.content;
}

function createStreamingMessage(id: string, runId?: string | null): ChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    createdAt: new Date().toISOString(),
    meta: runId ? { runId } : undefined
  };
}

class MessageRenderBoundary extends Component<
  { children: ReactNode; messageId: string },
  { failedMessageId: string | null }
> {
  state = { failedMessageId: null };

  static getDerivedStateFromError() {
    return { failedMessageId: "__failed__" };
  }

  componentDidCatch(error: unknown) {
    console.error("Message render failed", { messageId: this.props.messageId, error });
  }

  componentDidUpdate(previousProps: { messageId: string }) {
    if (previousProps.messageId !== this.props.messageId && this.state.failedMessageId) {
      this.setState({ failedMessageId: null });
    }
  }

  render() {
    if (this.state.failedMessageId) {
      return (
        <article className="message-row assistant">
          <div className="avatar">K</div>
          <div className="message-body">
            <header><strong>K Agent</strong></header>
            <div className="bubble">这条历史消息暂时无法渲染。</div>
          </div>
        </article>
      );
    }

    return this.props.children;
  }
}

type SpeechRecognitionLike = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onresult: ((event: { results: ArrayLike<{ [index: number]: { transcript: string } }> }) => void) | null;
  start: () => void;
  stop: () => void;
};

function getSpeechRecognitionConstructor(): (new () => SpeechRecognitionLike) | null {
  // Safari still exposes the prefixed constructor while Chromium exposes the
  // standard name; Firefox currently provides neither in regular releases.
  const speechWindow = window as typeof window & {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  };
  return speechWindow.SpeechRecognition ?? speechWindow.webkitSpeechRecognition ?? null;
}

function splitSpeakableChunks(text: string, force: boolean): { ready: string[]; remainder: string } {
  const ready: string[] = [];
  let start = 0;
  const boundary = /[。！？!?；;：:\n]/;

  for (let index = 0; index < text.length; index += 1) {
    const size = index - start + 1;
    const shouldCutAtPunctuation = boundary.test(text[index]) && size >= 10;
    const shouldCutByLength = size >= 90;
    if (!shouldCutAtPunctuation && !shouldCutByLength) continue;
    ready.push(text.slice(start, index + 1));
    start = index + 1;
  }

  const remainder = text.slice(start);
  if (force && remainder.trim()) {
    ready.push(remainder);
    return { ready, remainder: "" };
  }
  return { ready, remainder };
}

function splitTextForManualSpeech(content: string): string[] {
  const chunks = splitSpeakableChunks(content, true).ready
    .map((chunk) => sanitizeSpeechText(chunk))
    .filter((chunk) => chunk.length > 0);
  return chunks.length ? chunks : sanitizeSpeechText(content) ? [sanitizeSpeechText(content)] : [];
}

function sanitizeSpeechText(text: string): string {
  // Browser TTS reads literal markdown punctuation poorly. The UI still renders
  // markdown normally; this cleanup only prepares client-side speech text.
  return text
    .replace(/```[\s\S]*?```/g, " 代码块 ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "链接")
    .replace(/^[#>*\-\s]+/gm, "")
    .replace(/[*_~]{1,3}/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function formatRelativeTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知时间";
  const delta = Date.now() - date.getTime();
  if (delta < 60_000) return "刚刚";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function thinkingIcon(phase: ThinkingActivity["phase"]) {
  if (phase === "tool") return "⌁";
  if (phase === "synthesis" || phase === "complete") return "✓";
  return "✦";
}

function Typing() {
  return <span className="typing"><i /><i /><i /></span>;
}

function InlineThinking({ block }: { block: ThinkingBlock }) {
  const { steps } = block;
  const latestActive = [...steps].reverse().find((step) => step.status === "active") ?? steps[steps.length - 1];
  const hasActive = !block.closed && steps.some((step) => step.status === "active");
  const [open, setOpen] = useState(!block.closed);

  useEffect(() => {
    setOpen(!block.closed);
  }, [block.closed, block.id]);

  return (
    <section className={`inline-thinking ${open ? "open" : ""}`}>
      <button
        type="button"
        className="inline-thinking-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{hasActive ? "思考过程" : "已完成思考"}</span>
        {latestActive?.status === "active" && <b>进行中</b>}
        <i aria-hidden="true">⌃</i>
      </button>
      {open && (
        <div className="inline-thinking-list">
          {steps.map((step) => (
            <article className={`inline-thinking-step ${step.status}`} key={step.id}>
              <span>{thinkingIcon(step.phase)}</span>
              <div>
                <strong>{step.title}</strong>
                {step.detail && <p>{step.detail}</p>}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function ApprovalCard({
  approval,
  onDecision
}: {
  approval: ApprovalActivity;
  onDecision: (
    approval: ApprovalActivity,
    action: "approve" | "deny" | "cancel",
    remember?: boolean
  ) => Promise<void>;
}) {
  const pending = approval.status === "pending" || approval.status === "error";
  const submitting = approval.status === "submitting";
  const argumentsValue = approval.detail.arguments;
  const command = approval.detail.command;
  const preview = command
    ? String(command)
    : argumentsValue !== undefined
      ? safeStringify(argumentsValue)
      : "";
  const statusText = {
    pending: "等待确认",
    submitting: "正在提交",
    approved: "已允许",
    denied: "已拒绝",
    cancelled: "已取消",
    error: "提交失败"
  }[approval.status];

  return (
    <section className={`approval-card ${approval.status}`} aria-live="polite">
      <header>
        <span className="approval-shield" aria-hidden="true">!</span>
        <div>
          <small>{approval.agentKind} · {approval.category}</small>
          <strong>{approval.title}</strong>
        </div>
        <b>{statusText}</b>
      </header>
      <p>{approval.message}</p>
      {preview && <code>{preview}</code>}
      {approval.error && <p className="approval-error">{approval.error}</p>}
      {pending && (
        <footer>
          <button type="button" disabled={submitting} onClick={() => void onDecision(approval, "deny")}>拒绝</button>
          <button type="button" disabled={submitting} onClick={() => void onDecision(approval, "approve")}>允许一次</button>
          <button className="primary" type="button" disabled={submitting} onClick={() => void onDecision(approval, "approve", true)}>本轮始终允许</button>
        </footer>
      )}
    </section>
  );
}

function InspectorSection({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return (
    <section className="inspector-section">
      <header>
        <h3>{title}</h3>
        <span>{count}</span>
      </header>
      {children}
    </section>
  );
}

function EmptyInspector({ text }: { text: string }) {
  return (
    <div className="inspector-empty">
      <span>—</span>
      <p>{text}</p>
    </div>
  );
}

function ToolIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M11.7 4.1a4.1 4.1 0 0 0-4.9 5.3l-4.4 4.4a1.7 1.7 0 1 0 2.4 2.4l4.4-4.4a4.1 4.1 0 0 0 5.3-4.9l-2.5 2.5-1.9-1.9 2.5-2.5a4 4 0 0 0-.9-.9Z" />
    </svg>
  );
}

function SidebarIcon({ side }: { side: "left" | "right" }) {
  return (
    <svg className={side === "right" ? "mirrored" : ""} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="5" y="5" width="14" height="14" rx="2.5" />
      <path d="M10 6v12" />
    </svg>
  );
}

function ComposerAddMenu({
  imageEnabled,
  onImages,
  mcpItems,
  selectedMcp,
  onMcpChange,
  skillItems,
  selectedSkills,
  onSkillsChange
}: {
  imageEnabled: boolean;
  onImages: (files: FileList | null) => void;
  mcpItems: Array<{ id: string; name: string; description?: string }>;
  selectedMcp: string[];
  onMcpChange: (ids: string[]) => void;
  skillItems: Array<{ id: string; name: string; description?: string }>;
  selectedSkills: string[];
  onSkillsChange: (ids: string[]) => void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const closeOnOutsidePointerDown = (event: globalThis.PointerEvent) => {
      if (open && menuRef.current && !menuRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointerDown);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointerDown);
  }, [open]);

  const renderSelection = (
    item: { id: string; name: string; description?: string },
    selected: string[],
    onChange: (ids: string[]) => void
  ) => {
    const checked = selected.includes(item.id);
    return (
      <label className={`composer-menu-option ${checked ? "selected" : ""}`} key={item.id}>
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked
            ? [...selected, item.id]
            : selected.filter((id) => id !== item.id))}
        />
        <span><strong>{item.name}</strong>{item.description && <small>{item.description}</small>}</span>
        <i>{checked ? "✓" : ""}</i>
      </label>
    );
  };

  return (
    <div
      ref={menuRef}
      className={`composer-add-menu ${open ? "open" : ""}`}
      onKeyDown={(event) => {
        if (event.key === "Escape") setOpen(false);
      }}
    >
      {open && (
        <section className="composer-add-popover" aria-label="添加内容与能力">
          <header>添加</header>
          <label className={`composer-menu-option composer-file-option ${imageEnabled ? "" : "disabled"}`}>
            <input
              type="file"
              accept="image/*"
              multiple
              disabled={!imageEnabled}
              onChange={(event) => {
                onImages(event.target.files);
                event.target.value = "";
                setOpen(false);
              }}
            />
            <b>＋</b><span><strong>图片</strong><small>{imageEnabled ? "添加一张或多张图片" : "当前 Agent 不支持图片"}</small></span>
          </label>
          <div className="composer-menu-section">
            <header><span>MCP</span><small>{selectedMcp.length} 已选择</small></header>
            {mcpItems.length === 0 && <p>暂无可用 MCP</p>}
            {mcpItems.map((item) => renderSelection(item, selectedMcp, onMcpChange))}
          </div>
          <div className="composer-menu-section">
            <header><span>Skills</span><small>{selectedSkills.length} 已选择</small></header>
            {skillItems.length === 0 && <p>暂无可用 Skill</p>}
            {skillItems.map((item) => renderSelection(item, selectedSkills, onSkillsChange))}
          </div>
        </section>
      )}
      <button className="composer-add-trigger" type="button" aria-expanded={open} aria-haspopup="menu" onClick={() => setOpen((value) => !value)}>
        <span>＋</span><strong>添加</strong>
        {(selectedMcp.length + selectedSkills.length) > 0 && <i>{selectedMcp.length + selectedSkills.length}</i>}
      </button>
    </div>
  );
}

const reasoningOptions = [
  { id: "none", name: "none", hint: "" },
  { id: "low", name: "low", hint: "" },
  { id: "medium", name: "medium", hint: "" },
  { id: "high", name: "high", hint: "" },
  { id: "max", name: "max", hint: "" }
] as const;

type ReasoningModel = Pick<ModelProfile, "id" | "supportsReasoning"> &
  Partial<Pick<ModelProfile, "name" | "model" | "baseUrl">>;

function isDeepSeekModel(model?: ReasoningModel) {
  const marker = `${model?.id ?? ""} ${model?.name ?? ""} ${model?.model ?? ""} ${model?.baseUrl ?? ""}`.toLowerCase();
  return marker.includes("deepseek");
}

function reasoningOptionsForModel(model?: ReasoningModel) {
  if (!model?.supportsReasoning) return [reasoningOptions[0]];
  // DeepSeek 只开放 high/max 两档思考强度，避免前端提交 low/medium 造成后端实际调用失败。
  if (isDeepSeekModel(model)) {
    return reasoningOptions.filter((option) => option.id === "none" || option.id === "high" || option.id === "max");
  }
  return reasoningOptions.filter((option) => option.id !== "max");
}

function modelsForAgent(
  kind: AgentKind,
  kAgentModels: ModelProfile[],
  agents: DetectedAgent[]
): Array<Pick<ModelProfile, "id" | "name" | "supportsReasoning" | "multimodal">> {
  if (kind === "k_agent") {
    return kAgentModels.map((model) => ({
      id: model.id,
      name: model.name,
      supportsReasoning: model.supportsReasoning,
      multimodal: model.multimodal
    }));
  }
  const agent = agents.find((item) => item.kind === kind);
  return (agent?.models || []).map((model) => ({
    id: model.id,
    name: model.name,
    supportsReasoning: Boolean(model.supportsReasoning),
    multimodal: false
  }));
}

function resolveModelForAgent(
  kind: AgentKind,
  kAgentModels: ModelProfile[],
  agents: DetectedAgent[]
): string {
  const options = modelsForAgent(kind, kAgentModels, agents);
  const remembered = readModelByAgent()[kind];
  if (remembered && options.some((model) => model.id === remembered)) return remembered;
  if (kind === "k_agent") return options[0]?.id ?? "";
  const agent = agents.find((item) => item.kind === kind);
  if (agent?.defaultModelId && options.some((model) => model.id === agent.defaultModelId)) {
    return agent.defaultModelId;
  }
  return options[0]?.id ?? "";
}

function RuntimeSettingsPicker({
  agentKind,
  agents,
  cliSessionMode,
  modelId,
  kAgentModels,
  reasoningEffort,
  onRuntimeChange,
  onCliSessionModeChange,
  onReasoningChange
}: {
  agentKind: AgentKind;
  agents: DetectedAgent[];
  cliSessionMode: CliSessionMode;
  modelId: string;
  kAgentModels: ModelProfile[];
  reasoningEffort: ReasoningEffort;
  onRuntimeChange: (nextAgent: AgentKind, nextModel: string) => void;
  onCliSessionModeChange: (value: CliSessionMode) => void;
  onReasoningChange: (value: ReasoningEffort) => void;
}) {
  const pickerRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<{ agentKind: AgentKind; modelId: string } | null>(null);
  const currentAgent = agents.find((agent) => agent.kind === agentKind) ?? agents[0];
  const currentModels = modelsForAgent(agentKind, kAgentModels, agents);
  const currentModel = currentModels.find((model) => model.id === modelId);
  const availableReasoningOptions = reasoningOptionsForModel(currentModel);
  const normalizedReasoning = availableReasoningOptions.some((option) => option.id === reasoningEffort) ? reasoningEffort : "none";
  const currentReasoning = reasoningOptions.find((option) => option.id === normalizedReasoning);
  const supportsReasoning = Boolean(currentModel?.supportsReasoning);
  const previewAgentKind = preview?.agentKind ?? agentKind;
  const previewModelId = preview?.modelId ?? modelId;
  const previewAgent = agents.find((agent) => agent.kind === previewAgentKind) ?? currentAgent;
  const previewModel = modelsForAgent(previewAgentKind, kAgentModels, agents).find((model) => model.id === previewModelId) ?? currentModel;
  const previewReasoningOptions = reasoningOptionsForModel(previewModel);

  useEffect(() => {
    if (normalizedReasoning !== reasoningEffort) onReasoningChange(normalizedReasoning);
  }, [normalizedReasoning, reasoningEffort, onReasoningChange]);

  useEffect(() => {
    const closeOnOutsidePointerDown = (event: globalThis.PointerEvent) => {
      if (open && pickerRef.current && !pickerRef.current.contains(event.target as Node)) {
        setOpen(false);
        setPreview(null);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointerDown);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointerDown);
  }, [open]);

  const close = () => {
    setOpen(false);
    setPreview(null);
  };

  return (
    <div
      ref={pickerRef}
      className={`runtime-settings ${open ? "open" : ""}`}
      onKeyDown={(event) => {
        if (event.key === "Escape") close();
      }}
    >
      {open && (
        <div className="runtime-settings-popovers">
          <section className="runtime-detail-card">
            <header><strong>{previewModel?.name ?? "选择模型"}</strong><small>{previewAgent?.name ?? "Agent"}</small></header>
            {previewAgent?.version && <p>{previewAgent.version}</p>}
            <div className="runtime-detail-row">
              <span>思考强度</span>
              <div>
                {previewReasoningOptions.map((option) => (
                  <button
                    className={previewAgentKind === agentKind && previewModelId === modelId && option.id === reasoningEffort ? "selected" : ""}
                    key={option.id}
                    type="button"
                    onClick={() => {
                      if (previewAgentKind !== agentKind || previewModelId !== modelId) onRuntimeChange(previewAgentKind, previewModelId);
                      onReasoningChange(option.id);
                    }}
                  >{option.name}</button>
                ))}
              </div>
            </div>
            {previewAgent?.supports_resume && (
              <div className="runtime-detail-row">
                <span>会话模式</span>
                <div>
                  {([['ephemeral', '新建'], ['resume', '继续']] as const).map(([id, name]) => (
                    <button className={id === cliSessionMode ? "selected" : ""} key={id} type="button" onClick={() => onCliSessionModeChange(id)}>{name}</button>
                  ))}
                </div>
              </div>
            )}
          </section>
          <section className="runtime-settings-card">
            {agents.filter((agent) => agent.available).map((agent) => {
              const agentModels = modelsForAgent(agent.kind, kAgentModels, agents);
              return (
                <div className="runtime-agent-group" key={agent.kind}>
                  <header><strong>{agent.name}</strong>{agent.version && <small>{agent.version}</small>}</header>
                  {agentModels.map((model) => {
                    const selected = agent.kind === agentKind && model.id === modelId;
                    return (
                      <button
                        className={selected ? "selected" : ""}
                        key={`${agent.kind}:${model.id}`}
                        type="button"
                        onPointerEnter={() => setPreview({ agentKind: agent.kind, modelId: model.id })}
                        onFocus={() => setPreview({ agentKind: agent.kind, modelId: model.id })}
                        onClick={() => onRuntimeChange(agent.kind, model.id)}
                      >
                        <span>
                          <strong>{model.name}</strong>
                          {selected && (model.supportsReasoning || agent.supports_resume) && (
                            <small>
                              {model.supportsReasoning ? `思考强度 ${currentReasoning?.name ?? "none"}` : ""}
                              {model.supportsReasoning && agent.supports_resume ? " · " : ""}
                              {agent.supports_resume ? (cliSessionMode === "resume" ? "继续上次会话" : "新建会话") : ""}
                            </small>
                          )}
                        </span>
                        <i>{selected ? "✓" : ""}</i>
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </section>
        </div>
      )}
      <button className="runtime-settings-trigger" type="button" aria-expanded={open} aria-haspopup="menu" onClick={() => {
        setOpen((value) => {
          const next = !value;
          setPreview(next ? { agentKind, modelId } : null);
          return next;
        });
      }}>
        <strong>{currentAgent?.name || "K Agent"}</strong><span>{currentModel?.name ?? "选择模型"}</span>
        {supportsReasoning && <em>{currentReasoning?.name}</em>}<i>⌃</i>
      </button>
    </div>
  );
}
