import {
  Component,
  FormEvent,
  KeyboardEvent,
  PointerEvent as ReactPointerEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import {
  getHealth,
  getMcpConfig,
  getModelsConfig,
  getSession,
  getSkillsConfig,
  listSessions,
  streamAgentRun
} from "./api/agui";
import { MarkdownContent } from "./components/MarkdownContent";
import { ConfigCenter } from "./components/ConfigCenter";
import { appConfig } from "./config";
import type {
  AgUiEvent,
  AgUiRunInput,
  ChatMessage,
  HealthState,
  McpServerConfig,
  ModelProfile,
  ReasoningEffort,
  SessionSummary,
  SkillConfig,
  TextActivity,
  ThinkingActivity,
  ToolActivity
} from "./types";

const suggestions = [
  "帮我拆解这个需求，并给出执行计划",
  "总结当前项目结构和关键模块",
  "检查可用工具，并说明能完成什么"
];

const colorThemes = [
  { id: "snow", name: "雪白", colors: ["#f4f6f2", "#ffffff", "#5f8f1f"] },
  { id: "forest", name: "深林", colors: ["#0d0f0e", "#171a18", "#c8f54a"] },
  { id: "ocean", name: "深海", colors: ["#081018", "#111f2b", "#55dff2"] },
  { id: "sand", name: "暖砂", colors: ["#17120d", "#251c14", "#f5ba4a"] },
  { id: "dusk", name: "暮色", colors: ["#160f16", "#251824", "#ff806e"] }
] as const;
type ColorTheme = typeof colorThemes[number]["id"];
type ThinkingGroup = {
  id: string;
  steps: ThinkingActivity[];
  closed: boolean;
  textStart: number;
  textEnd: number;
  sequence: number;
};

type InlineActivity =
  | { type: "thinking"; sequence: number; group: ThinkingGroup }
  | { type: "tool"; sequence: number; tool: ToolActivity }
  | { type: "text"; sequence: number; text: TextActivity };

type CopyFeedback = {
  messageId: string;
  status: "success" | "error";
};

const createMessage = (role: ChatMessage["role"], content: string): ChatMessage => ({
  id: crypto.randomUUID(),
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

function normalizeThinkingGroups(value: unknown, fallbackSteps: ThinkingActivity[], fallbackId: string): ThinkingGroup[] {
  if (!Array.isArray(value)) {
    return fallbackSteps.length ? [{ id: fallbackId, steps: fallbackSteps, closed: true, textStart: 0, textEnd: 0, sequence: 0 }] : [];
  }
  const stepOwners = new Map<string, number>();
  const groups: ThinkingGroup[] = [];
  value.forEach((group, index) => {
    if (!group || typeof group !== "object") return [];
    const record = group as Record<string, unknown>;
    const steps = normalizeThinking(record.steps);
    if (!steps.length) return;
    const groupIndex = groups.length;
    const uniqueSteps = steps.filter((step) => {
      const owner = stepOwners.get(step.id);
      if (owner === undefined) {
        stepOwners.set(step.id, groupIndex);
        return true;
      }
      // 修复旧数据中同一个 step 被保存到多个分组导致的重复折叠块。
      groups[owner].steps = groups[owner].steps.map((item) => item.id === step.id ? step : item);
      return false;
    });
    if (!uniqueSteps.length) return;
    groups.push({
      id: typeof record.id === "string" && record.id ? record.id : `${fallbackId}-thinking-${index}`,
      steps: uniqueSteps,
      closed: typeof record.closed === "boolean" ? record.closed : true,
      textStart: typeof record.textStart === "number" ? record.textStart : 0,
      textEnd: typeof record.textEnd === "number" ? record.textEnd : 0,
      sequence: typeof record.sequence === "number" ? record.sequence : index * 2
    });
  });
  return groups;
}

function normalizeToolActivities(value: unknown): ToolActivity[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((tool, index) => {
    if (!tool || typeof tool !== "object") return [];
    const record = tool as Record<string, unknown>;
    if (typeof record.id !== "string" || typeof record.name !== "string") return [];
    return [{
      id: record.id,
      name: record.name,
      arguments: typeof record.arguments === "string" ? record.arguments : "",
      result: typeof record.result === "string" ? record.result : undefined,
      status: record.status === "complete" ? "complete" as const : "running" as const,
      sequence: typeof record.sequence === "number" ? record.sequence : index * 2 + 1,
      textOffset: typeof record.textOffset === "number" ? record.textOffset : 0
    }];
  });
}

function normalizeTextActivities(value: unknown, fallbackContent = ""): TextActivity[] {
  if (!Array.isArray(value)) {
    return fallbackContent ? [{ id: "text-fallback", content: fallbackContent, status: "complete", sequence: 0 }] : [];
  }
  const texts = value.flatMap((text, index) => {
    if (!text || typeof text !== "object") return [];
    const record = text as Record<string, unknown>;
    const content = typeof record.content === "string" ? record.content : "";
    if (!content) return [];
    return [{
      id: typeof record.id === "string" && record.id ? record.id : `text-${index}`,
      content,
      status: record.status === "streaming" ? "streaming" as const : "complete" as const,
      sequence: typeof record.sequence === "number" ? record.sequence : index * 2
    }];
  });
  return texts.length ? texts : fallbackContent ? [{ id: "text-fallback", content: fallbackContent, status: "complete", sequence: 0 }] : [];
}

function inlineTimeline(texts: TextActivity[], groups: ThinkingGroup[], tools: ToolActivity[]): InlineActivity[] {
  return [
    ...texts.map((text) => ({ type: "text" as const, sequence: text.sequence, text })),
    ...groups.map((group) => ({ type: "thinking" as const, sequence: group.sequence, group })),
    ...tools.map((tool, index) => ({ type: "tool" as const, sequence: tool.sequence ?? index * 2 + 1, tool }))
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

export function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [trace, setTrace] = useState<string[]>([]);
  const [tasks, setTasks] = useState<string[]>([]);
  const [thinking, setThinking] = useState<ThinkingActivity[]>([]);
  const [thinkingGroups, setThinkingGroups] = useState<ThinkingGroup[]>([]);
  const [tools, setTools] = useState<ToolActivity[]>([]);
  const [textActivities, setTextActivities] = useState<TextActivity[]>([]);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [status, setStatus] = useState<string>(appConfig.status.idle);
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem("k-agent-sidebar-collapsed") === "true");
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [sidebarWidth, setSidebarWidth] = useState(() => Number(localStorage.getItem("k-agent-sidebar-width")) || 252);
  const [inspectorWidth, setInspectorWidth] = useState(() => Number(localStorage.getItem("k-agent-inspector-width")) || 310);
  const [view, setView] = useState<"chat" | "config">("chat");
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerConfig[]>([]);
  const [skills, setSkills] = useState<SkillConfig[]>([]);
  const [modelId, setModelId] = useState("");
  const [selectedMcp, setSelectedMcp] = useState<string[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("none");
  const [attachments, setAttachments] = useState<Array<{ name: string; dataUrl: string; type: string }>>([]);
  const [listening, setListening] = useState(false);
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback | null>(null);
  const composerTextareaRef = useRef<HTMLTextAreaElement>(null);
  const [colorTheme, setColorTheme] = useState<ColorTheme>(() => {
    const savedTheme = localStorage.getItem("k-agent-color-theme");
    return colorThemes.some((theme) => theme.id === savedTheme) ? savedTheme as ColorTheme : "snow";
  });
  const streamEndRef = useRef<HTMLDivElement>(null);
  const messageStreamRef = useRef<HTMLElement>(null);
  const streamPinnedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);
  const speechRef = useRef<{ start: () => void; stop: () => void } | null>(null);
  const pendingAssistantIdRef = useRef<string | null>(null);
  const activeTextActivityIdRef = useRef<string | null>(null);
  const activeTextMessageIdRef = useRef<string | null>(null);
  const activeThinkingGroupIdRef = useRef<string | null>(null);
  const activeThinkingStepIdRef = useRef<string | null>(null);
  const activeThinkingTitleRef = useRef("思考过程");
  const assistantVisibleTextLengthRef = useRef(0);
  const activitySequenceRef = useRef(0);
  const copyFeedbackTimerRef = useRef<number | null>(null);

  useEffect(() => {
    void refreshSessions();
    void refreshHealth();
    void refreshOptions();
    const savedId = localStorage.getItem(appConfig.storageKey);
    if (savedId) void openSession(savedId);
    return () => {
      abortRef.current?.abort();
      if (copyFeedbackTimerRef.current !== null) {
        window.clearTimeout(copyFeedbackTimerRef.current);
      }
    };
  }, []);

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
  }, [messages, tools, loading]);

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
    localStorage.setItem("k-agent-sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  const filteredSessions = sessions.filter((session) =>
    session.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  );
  const activeSession = sessions.find((session) => session.id === sessionId);
  const visibleMessages = messages.filter((message) =>
    message.role === "user" || message.role === "assistant"
  );
  const displayMessages = visibleMessages.filter((message, index) =>
    !(message.role === "assistant" && !message.content && visibleMessages.slice(index + 1).some((next) => next.role === "assistant"))
  );
  const lastAssistantMessageId = [...displayMessages].reverse().find((message) => message.role === "assistant")?.id;

  async function refreshHealth() {
    try {
      setHealth(await getHealth());
    } catch {
      setHealth(null);
    }
  }

  async function refreshSessions() {
    try {
      setSessions(normalizeSessionSummaries(await listSessions()));
    } catch {
      setSessions([]);
    }
  }

  async function refreshOptions() {
    try {
      const [modelData, mcpData, skillData] = await Promise.all([
        getModelsConfig(), getMcpConfig(), getSkillsConfig()
      ]);
      const enabledModels = modelData.models.filter((model) => model.enabled);
      const enabledMcp = mcpData.servers.filter((server) => server.enabled);
      const enabledSkills = skillData.skills.filter((skill) => skill.enabled);
      setModels(enabledModels);
      setMcpServers(enabledMcp);
      setSkills(enabledSkills);
      setModelId((current) => enabledModels.some((model) => model.id === current) ? current : enabledModels[0]?.id ?? "");
      setSelectedMcp((current) => current.length ? current.filter((id) => enabledMcp.some((server) => server.id === id)) : enabledMcp.map((server) => server.id));
      setSelectedSkills((current) => current.filter((id) => enabledSkills.some((skill) => skill.id === id)));
    } catch {
      setModels([]);
      setMcpServers([]);
      setSkills([]);
    }
  }

  async function openSession(nextId: string) {
    if (loading) return;
    try {
      const data = await getSession(nextId);
      setSessionId(data.sessionId);
      localStorage.setItem(appConfig.storageKey, data.sessionId);
      const normalizedMessages = normalizeMessages(data.messages);
      const normalizedThinking = normalizeThinking(data.thinking);
      setMessages(normalizedMessages);
      setTrace(normalizeStringList(data.trace));
      setTasks(normalizeStringList(data.tasks));
      setThinking(normalizedThinking);
      setThinkingGroups(normalizeThinkingGroups(data.thinkingGroups, normalizedThinking, data.sessionId));
      activeThinkingGroupIdRef.current = null;
      activeThinkingStepIdRef.current = null;
      activitySequenceRef.current = 0;
      const latestAssistant = [...normalizedMessages].reverse().find((message) => message.role === "assistant");
      setTools(normalizeToolActivities(latestAssistant?.meta?.toolActivities));
      setTextActivities(normalizeTextActivities(latestAssistant?.meta?.textActivities, latestAssistant?.content ?? ""));
      setStatus(appConfig.status.idle);
      setSidebarOpen(false);
    } catch {
      localStorage.removeItem(appConfig.storageKey);
      setStatus("会话已失效");
    }
  }

  function startNewSession() {
    if (loading) return;
    localStorage.removeItem(appConfig.storageKey);
    setSessionId(null);
    setMessages([]);
    setTrace([]);
    setTasks([]);
    setThinking([]);
    setThinkingGroups([]);
    setTextActivities([]);
    activeThinkingGroupIdRef.current = null;
    activeThinkingStepIdRef.current = null;
    activeTextActivityIdRef.current = null;
    activeTextMessageIdRef.current = null;
    activitySequenceRef.current = 0;
    setTools([]);
    setStatus(appConfig.status.idle);
    setSidebarOpen(false);
  }

  function stopRun() {
    abortRef.current?.abort();
    abortRef.current = null;
    setLoading(false);
    setStatus("已停止");
  }

  async function copyMessage(messageId: string, content: string) {
    if (copyFeedbackTimerRef.current !== null) {
      window.clearTimeout(copyFeedbackTimerRef.current);
    }

    try {
      await navigator.clipboard.writeText(content);
      setCopyFeedback({ messageId, status: "success" });
    } catch {
      setCopyFeedback({ messageId, status: "error" });
    }

    copyFeedbackTimerRef.current = window.setTimeout(() => {
      setCopyFeedback((current) => current?.messageId === messageId ? null : current);
      copyFeedbackTimerRef.current = null;
    }, 1800);
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
        : Math.min(460, Math.max(250, startWidth - delta));
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
      const next = Math.min(460, Math.max(250, inspectorWidth - direction * 10));
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

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = input.trim();
    if ((!prompt && !attachments.length) || loading || !modelId) return;

    const selectedModel = models.find((model) => model.id === modelId);
    const validReasoningOptions = reasoningOptionsForModel(selectedModel);
    const effectiveReasoningEffort = validReasoningOptions.some((option) => option.id === reasoningEffort) ? reasoningEffort : "none";
    const pendingAssistantId = crypto.randomUUID();
    const nextMessages = [...messages, createMessage("user", prompt), createStreamingMessage(pendingAssistantId)];
    const runInput: AgUiRunInput = {
      threadId: sessionId ?? crypto.randomUUID(),
      runId: crypto.randomUUID(),
      state: { sessionId, trace, tasks },
      messages: nextMessages
        .filter((message) => message.role !== "tool")
        .map(({ id, role, content }) => ({ id, role, content })),
      tools: [],
      context: [],
      forwardedProps: {
        modelId,
        mcpServerIds: selectedMcp,
        skillIds: selectedSkills,
        reasoningEffort: effectiveReasoningEffort,
        attachments
      }
    };

    const controller = new AbortController();
    abortRef.current = controller;
    streamPinnedRef.current = true;
    setMessages(nextMessages);
    pendingAssistantIdRef.current = pendingAssistantId;
    setInput("");
    setAttachments([]);
    setTrace([]);
    setThinking([]);
    setThinkingGroups([]);
    setTextActivities([]);
    activeThinkingGroupIdRef.current = null;
    activeThinkingStepIdRef.current = null;
    activeTextActivityIdRef.current = null;
    activeTextMessageIdRef.current = null;
    assistantVisibleTextLengthRef.current = 0;
    activitySequenceRef.current = 0;
    setTools([]);
    setLoading(true);
    setStatus(appConfig.status.preparing);

    try {
      await streamAgentRun(runInput, applyAgUiEvent, controller.signal);
      await Promise.all([refreshSessions(), refreshHealth()]);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      const detail = error instanceof Error ? error.message : "未知错误";
      setStatus(appConfig.status.failed);
      const pendingAssistantId = pendingAssistantIdRef.current;
      pendingAssistantIdRef.current = null;
      setMessages((current) =>
        pendingAssistantId && current.some((message) => message.id === pendingAssistantId)
          ? current.map((message) =>
            message.id === pendingAssistantId
              ? { ...message, content: `${appConfig.text.requestErrorPrefix}${detail}` }
              : message
          )
          : [...current, createMessage("assistant", `${appConfig.text.requestErrorPrefix}${detail}`)]
      );
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setLoading(false);
    }
  }

  function applyAgUiEvent(event: AgUiEvent) {
    switch (event.type) {
      case "RUN_STARTED":
        setSessionId(event.threadId);
        localStorage.setItem(appConfig.storageKey, event.threadId);
        setStatus(appConfig.status.processing);
        break;
      case "TEXT_MESSAGE_START":
        activeTextMessageIdRef.current = event.messageId;
        assistantVisibleTextLengthRef.current = 0;
        activitySequenceRef.current += 1;
        activeTextActivityIdRef.current = event.messageId;
        setTextActivities((current) => [
          ...current,
          { id: event.messageId, content: "", status: "streaming", sequence: activitySequenceRef.current }
        ]);
        setMessages((current) => {
          const pendingAssistantId = pendingAssistantIdRef.current;
          const fallbackPendingId = [...current].reverse().find((message) => message.role === "assistant" && !message.content)?.id;
          const reusableAssistantId = pendingAssistantId && current.some((message) => message.id === pendingAssistantId)
            ? pendingAssistantId
            : fallbackPendingId;
          if (reusableAssistantId) {
            pendingAssistantIdRef.current = null;
            return current.map((message) =>
              message.id === reusableAssistantId ? { ...message, id: event.messageId } : message
            );
          }
          return current.some((message) => message.id === event.messageId)
            ? current
            : [...current, createStreamingMessage(event.messageId)];
        });
        break;
      case "TEXT_MESSAGE_END":
        if (activeTextMessageIdRef.current === event.messageId) {
          activeTextMessageIdRef.current = null;
        }
        if (activeTextActivityIdRef.current === event.messageId) {
          activeTextActivityIdRef.current = null;
        }
        setTextActivities((current) => current.map((text) =>
          text.id === event.messageId ? { ...text, status: "complete" } : text
        ));
        break;
      case "TEXT_MESSAGE_CONTENT":
        {
          const visibleDeltaLength = visibleTextLength(event.delta);
          assistantVisibleTextLengthRef.current += visibleDeltaLength;
        }
        setTextActivities((current) => {
          if (current.some((text) => text.id === event.messageId)) {
            return current.map((text) =>
              text.id === event.messageId ? { ...text, content: text.content + event.delta } : text
            );
          }
          activitySequenceRef.current += 1;
          activeTextActivityIdRef.current = event.messageId;
          return [
            ...current,
            { id: event.messageId, content: event.delta, status: "streaming", sequence: activitySequenceRef.current }
          ];
        });
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
                ? { ...message, id: event.messageId, content: event.delta }
                : message
            )
            : [...current, { ...createStreamingMessage(event.messageId), content: event.delta }];
        });
        break;
      case "THINKING_START":
        activeThinkingTitleRef.current = event.title || "思考过程";
        activeThinkingStepIdRef.current = null;
        break;
      case "THINKING_TEXT_MESSAGE_START": {
        const rawStep = normalizeThinkingStep(event.rawEvent);
        const step = rawStep
          ? { ...rawStep, detail: "", status: "active" as const }
          : {
            id: crypto.randomUUID(),
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
        closeActiveThinkingGroup();
        break;
      case "TOOL_CALL_START":
        activitySequenceRef.current += 1;
        setTools((current) => [...current, {
          id: event.toolCallId,
          name: event.toolCallName,
          arguments: "",
          status: "preparing",
          sequence: activitySequenceRef.current,
          textOffset: assistantVisibleTextLengthRef.current
        }]);
        setStatus(`调用 ${event.toolCallName}`);
        break;
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
          ...tool, result: event.content, status: "complete"
        }));
        break;
      case "CUSTOM":
        if (event.name === "status") {
          setStatus(String(event.value.message ?? appConfig.status.processing));
        }
        if (event.name === "trace") {
          const entry = String(event.value.entry ?? "");
          if (entry) setTrace((current) => [...current, entry]);
        }
        break;
      case "STATE_SNAPSHOT": {
        pendingAssistantIdRef.current = null;
        const normalizedThinking = normalizeThinking(event.snapshot.thinking);
        setMessages(normalizeMessages(event.snapshot.messages));
        setTrace(normalizeStringList(event.snapshot.trace));
        setTasks(normalizeStringList(event.snapshot.tasks));
        setThinking(normalizedThinking);
        setThinkingGroups(normalizeThinkingGroups(event.snapshot.thinkingGroups, normalizedThinking, event.snapshot.sessionId));
        activeThinkingGroupIdRef.current = null;
        activeThinkingStepIdRef.current = null;
        activeTextActivityIdRef.current = null;
        activeTextMessageIdRef.current = null;
        const snapshotMessages = normalizeMessages(event.snapshot.messages);
        const latestAssistant = [...snapshotMessages].reverse().find((message) => message.role === "assistant");
        setTools(normalizeToolActivities(latestAssistant?.meta?.toolActivities));
        setTextActivities(normalizeTextActivities(latestAssistant?.meta?.textActivities, latestAssistant?.content ?? ""));
        break;
      }
      case "RUN_FINISHED":
        setStatus(appConfig.status.complete);
        break;
      case "RUN_ERROR": {
        const pendingAssistantId = pendingAssistantIdRef.current;
        pendingAssistantIdRef.current = null;
        setStatus(appConfig.status.failed);
        setMessages((current) =>
          pendingAssistantId && current.some((message) => message.id === pendingAssistantId)
            ? current.map((message) =>
              message.id === pendingAssistantId
                ? { ...message, content: `${appConfig.text.requestErrorPrefix}${event.message}` }
                : message
            )
            : [...current, createMessage("assistant", `${appConfig.text.requestErrorPrefix}${event.message}`)]
        );
        break;
      }
    }
  }

  function updateTool(id: string, transform: (tool: ToolActivity) => ToolActivity) {
    setTools((current) => current.map((tool) => tool.id === id ? transform(tool) : tool));
  }

  function upsertThinkingStep(step: ThinkingActivity) {
    setThinking((current) => {
      const exists = current.some((item) => item.id === step.id);
      return exists
        ? current.map((item) => item.id === step.id ? step : item)
        : [...current, step];
    });
    setThinkingGroups((current) => {
      const existingGroup = current.find((group) => group.steps.some((item) => item.id === step.id));
      if (existingGroup) {
        return current.map((group) =>
          group.id === existingGroup.id
            ? {
              ...group,
              textEnd: assistantVisibleTextLengthRef.current,
              steps: group.steps.map((item) => item.id === step.id ? step : item)
            }
            : group
        );
      }
      const activeGroupId = activeThinkingGroupIdRef.current;
      const activeGroup = activeGroupId ? current.find((group) => group.id === activeGroupId) : null;
      const groupId = activeGroup && !activeGroup.closed ? activeGroup.id : crypto.randomUUID();
      activeThinkingGroupIdRef.current = groupId;
      if (!activeGroup || activeGroup.closed) activitySequenceRef.current += 1;
      const targetGroups = activeGroup && !activeGroup.closed
        ? current
        : [...current, {
          id: groupId,
          steps: [],
          closed: false,
          textStart: assistantVisibleTextLengthRef.current,
          textEnd: assistantVisibleTextLengthRef.current,
          sequence: activitySequenceRef.current
        }];
      return targetGroups.map((group) => {
        if (group.id !== groupId) return group;
        const exists = group.steps.some((item) => item.id === step.id);
        return {
          ...group,
          textEnd: assistantVisibleTextLengthRef.current,
          steps: exists
            ? group.steps.map((item) => item.id === step.id ? step : item)
            : [...group.steps, step],
        };
      });
    });
  }

  function appendThinkingStepDetail(id: string, delta: string) {
    setThinking((current) => current.map((step) =>
      step.id === id ? { ...step, detail: step.detail + delta } : step
    ));
    setThinkingGroups((current) => current.map((group) => ({
      ...group,
      steps: group.steps.map((step) =>
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
    setThinkingGroups((current) => current.map((group) => ({
      ...group,
      steps: group.steps.map(complete)
    })));
  }

  function closeActiveThinkingGroup() {
    const activeGroupId = activeThinkingGroupIdRef.current;
    if (!activeGroupId) return;
    activeThinkingGroupIdRef.current = null;
    setThinkingGroups((current) => current.map((group) =>
      group.id === activeGroupId
        ? { ...group, closed: true, textEnd: assistantVisibleTextLengthRef.current }
        : group
    ));
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
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
    const SpeechRecognition = (
      window as typeof window & {
        SpeechRecognition?: new () => SpeechRecognitionLike;
        webkitSpeechRecognition?: new () => SpeechRecognitionLike;
      }
    ).SpeechRecognition ?? (
      window as typeof window & { webkitSpeechRecognition?: new () => SpeechRecognitionLike }
    ).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setStatus("当前浏览器不支持语音输入");
      return;
    }
    const recognition = new SpeechRecognition();
    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.continuous = false;
    const originalInput = input;
    recognition.onstart = () => setListening(true);
    recognition.onend = () => {
      setListening(false);
      speechRef.current = null;
    };
    recognition.onerror = () => {
      setListening(false);
      speechRef.current = null;
      setStatus("语音输入未能启动");
    };
    recognition.onresult = (event) => {
      let transcript = "";
      for (let index = 0; index < event.results.length; index += 1) {
        transcript += event.results[index][0]?.transcript ?? "";
      }
      setInput(`${originalInput}${originalInput && transcript ? " " : ""}${transcript}`);
    };
    speechRef.current = recognition;
    recognition.start();
  }

  if (view === "config") {
    return <ConfigCenter onBack={() => { setView("chat"); void refreshOptions(); }} />;
  }

  return (
    <div
      className={`shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${inspectorOpen ? "" : "inspector-hidden"}`}
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
          <span className="brand-symbol">K</span>
          <div><strong>K Agent</strong><small>智能任务工作台</small></div>
        </div>
        <button className="new-session" type="button" onClick={startNewSession}>
          <span>＋</span> 新建会话 <kbd>⌘ N</kbd>
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
              className={`session-item ${session.id === sessionId ? "active" : ""}`}
              key={session.id}
              type="button"
              onClick={() => void openSession(session.id)}
            >
              <span className="session-glyph">◫</span>
              <span className="session-copy">
                <strong>{session.title}</strong>
                <small>{formatRelativeTime(session.updatedAt)} · {session.messageCount} 条</small>
              </span>
            </button>
          ))}
        </nav>
        <button className="connection" type="button" onClick={() => void refreshHealth()}>
          <span className={`connection-dot ${health?.ok ? "online" : ""}`} />
          <span>
            <strong>{health?.ok ? "服务运行正常" : "后端未连接"}</strong>
            <small>{health ? `${health.model} · ${health.localToolCount + health.mcpToolCount} 个工具` : "点击重新检查"}</small>
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

      <main className="conversation">
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
              className="topbar-icon-button theme-button"
              type="button"
              aria-label="切换页面主题"
              title={`当前主题：${colorThemes.find((theme) => theme.id === colorTheme)?.name ?? "雪白"}，点击切换`}
              onClick={() => {
                const currentIndex = colorThemes.findIndex((theme) => theme.id === colorTheme);
                setColorTheme(colorThemes[(currentIndex + 1) % colorThemes.length].id);
              }}
            >
              <i />
            </button>
            <button className="topbar-icon-button" type="button" onClick={() => setInspectorOpen((value) => !value)} aria-label="切换运行详情">
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
              <div className="welcome-mark">K</div>
              <p className="eyebrow">K AGENT / READY</p>
              <h2>把复杂的事，<br /><em>交给 Agent。</em></h2>
              <p className="welcome-copy">我可以理解任务、调用工具并保留工作上下文。直接描述目标，我会从这里开始。</p>
              <div className="suggestions">
                {suggestions.map((suggestion, index) => (
                  <button key={suggestion} type="button" onClick={() => setInput(suggestion)}>
                    <span>0{index + 1}</span>{suggestion}<i>↗</i>
                  </button>
                ))}
              </div>
            </div>
          )}

          {displayMessages.length > 0 && displayMessages.map((message) => (
            <MessageRenderBoundary key={message.id} messageId={message.id}>
              <article className={`message-row ${message.role}`}>
                <div className="avatar">{message.role === "user" ? "你" : "K"}</div>
                <div className="message-body">
                  {message.role === "assistant" && (() => {
                    const storedGroups = normalizeThinkingGroups(
                      message.meta?.thinkingGroups,
                      [],
                      message.id
                    );
                    const storedTools = normalizeToolActivities(message.meta?.toolActivities);
                    const storedTexts = normalizeTextActivities(message.meta?.textActivities, message.content);
                    const groups = message.id === lastAssistantMessageId && loading
                      ? thinkingGroups
                      : storedGroups.length
                        ? storedGroups
                        : message.id === lastAssistantMessageId
                          ? thinkingGroups
                          : [];
                    const messageTools = message.id === lastAssistantMessageId && loading
                      ? tools
                        : storedTools.length
                        ? storedTools
                        : message.id === lastAssistantMessageId
                          ? tools
                          : [];
                    const messageTexts = message.id === lastAssistantMessageId && loading
                      ? textActivities
                      : storedTexts.length
                        ? storedTexts
                        : message.id === lastAssistantMessageId
                          ? textActivities
                          : [];
                    return inlineTimeline(messageTexts, groups, messageTools).map((activity) =>
                      activity.type === "thinking"
                        ? <InlineThinking group={activity.group} key={`thinking-${activity.group.id}`} />
                        : activity.type === "tool"
                          ? <InlineTool tool={activity.tool} key={`tool-${activity.tool.id}`} />
                          : (
                            <div className="assistant-output" key={`text-${activity.text.id}`}>
                              <MarkdownContent content={activity.text.content} />
                            </div>
                          )
                    );
                  })()}
                  {message.role === "assistant"
                    ? !message.content && <div className="assistant-output"><Typing /></div>
                    : (
                      <div className="bubble">
                        {message.content ? <MarkdownContent content={message.content} /> : <Typing />}
                      </div>
                    )}
                  {!(loading && message.role === "assistant" && message.id === lastAssistantMessageId) && (
                    <footer className="message-meta">
                      <time>{formatTime(message.createdAt)}</time>
                      {message.content && (
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
                      )}
                    </footer>
                  )}
                </div>
              </article>
            </MessageRenderBoundary>
          ))}
          <div ref={streamEndRef} />
        </section>

        <form className="composer" onSubmit={handleSubmit}>
          {attachments.length > 0 && <div className="attachment-list">{attachments.map((attachment, index) => <span key={`${attachment.name}-${index}`}>▧ {attachment.name}<button type="button" onClick={() => setAttachments(attachments.filter((_, i) => i !== index))}>×</button></span>)}</div>}
          <textarea
            ref={composerTextareaRef}
            aria-label="给 Agent 发送消息"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleComposerKeyDown}
            placeholder="描述任务，K Agent 会规划并执行…"
            rows={2}
          />
          <div className="composer-footer">
            <div className="composer-toolbar composer-toolbar-left">
              <label className={`attach-button ${models.find((model) => model.id === modelId)?.multimodal ? "" : "disabled"}`} title="添加图片">＋<span>图片</span><input type="file" accept="image/*" multiple onChange={(event) => { void addImages(event.target.files); event.target.value = ""; }} /></label>
              <Picker label={`MCP ${selectedMcp.length}`} items={mcpServers.map((server) => ({ id: server.id, name: server.id }))} selected={selectedMcp} onChange={setSelectedMcp} />
              <Picker label={`Skill ${selectedSkills.length}`} items={skills.map((skill) => ({ id: skill.id, name: skill.name }))} selected={selectedSkills} onChange={setSelectedSkills} />
            </div>
            <div className="composer-toolbar composer-toolbar-right">
              <ModelSettingsPicker
                modelId={modelId}
                models={models}
                reasoningEffort={reasoningEffort}
                onModelChange={(value) => {
                  setModelId(value);
                  const next = models.find((model) => model.id === value);
                  setReasoningEffort("none");
                  if (!next?.multimodal) setAttachments([]);
                }}
                onReasoningChange={setReasoningEffort}
              />
              <button className={`voice-button ${listening ? "listening" : ""}`} type="button" onClick={toggleVoiceInput} aria-label={listening ? "停止语音输入" : "语音输入"} title={listening ? "停止语音输入" : "语音输入"}>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z"/><path d="M18 11v1a6 6 0 0 1-12 0v-1M12 18v3M9 21h6"/></svg>
              </button>
              {loading ? (
                <button className="stop-button" type="button" onClick={stopRun} aria-label="停止生成">■</button>
              ) : (
                <button className="send-button" type="submit" disabled={(!input.trim() && !attachments.length) || !modelId} aria-label="发送">↑</button>
              )}
            </div>
          </div>
        </form>
        <p className="disclaimer">Agent 可能会犯错，关键结果请核实。</p>
      </main>

      <div
        className="resize-handle resize-handle-right"
        role="separator"
        tabIndex={0}
        aria-label="调整运行详情宽度"
        aria-orientation="vertical"
        onPointerDown={(event) => beginResize("inspector", event)}
        onKeyDown={(event) => resizeWithKeyboard("inspector", event)}
      />
      <aside className="inspector">
        <header className="inspector-header">
          <div><p className="eyebrow">RUN DETAILS</p><h2>运行详情</h2></div>
        </header>
        <section className="run-summary">
          <div><span className={`run-orb ${loading ? "running" : ""}`}>◆</span><strong>{loading ? "Agent 正在工作" : "等待新任务"}</strong></div>
          <p>{status}</p>
          <dl>
            <div><dt>工具调用</dt><dd>{tools.length}</dd></div>
            <div><dt>轨迹事件</dt><dd>{trace.length}</dd></div>
            <div><dt>思考阶段</dt><dd>{thinking.length}</dd></div>
          </dl>
        </section>
        <InspectorSection title="思考过程" count={thinking.length}>
          {thinking.length ? <div className="thinking-list">{thinking.map((step) => (
            <article className={`thinking-step ${step.status}`} key={step.id}>
              <span className="thinking-icon">{thinkingIcon(step.phase)}</span>
              <div><strong>{step.title}</strong><p>{step.detail}</p></div>
              <i />
            </article>
          ))}</div> : <EmptyInspector text="开始任务后会显示执行思路" />}
        </InspectorSection>
        <InspectorSection title="任务计划" count={tasks.length}>
          {tasks.length ? tasks.map((task, index) => (
            <div className="task-row" key={`${task}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span><p>{task}</p></div>
          )) : <EmptyInspector text="任务开始后会生成执行步骤" />}
        </InspectorSection>
        <InspectorSection title="工具活动" count={tools.length}>
          {tools.length ? tools.map((tool) => (
            <details className="tool-row" key={tool.id}>
              <summary><span>⌁</span><strong>{tool.name}</strong><i className={tool.status} /></summary>
              {tool.arguments && <code>{tool.arguments}</code>}
              {tool.result && <p>{tool.result}</p>}
            </details>
          )) : <EmptyInspector text="暂时没有工具调用" />}
        </InspectorSection>
        <InspectorSection title="执行轨迹" count={trace.length}>
          {trace.length ? <ol className="timeline">{trace.map((entry, index) => <li key={`${entry}-${index}`}>{entry}</li>)}</ol> : <EmptyInspector text="运行轨迹会实时显示" />}
        </InspectorSection>
      </aside>
    </div>
  );
}

function createStreamingMessage(id: string): ChatMessage {
  return { id, role: "assistant", content: "", createdAt: new Date().toISOString() };
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
  onerror: (() => void) | null;
  onresult: ((event: { results: ArrayLike<{ [index: number]: { transcript: string } }> }) => void) | null;
  start: () => void;
  stop: () => void;
};

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

function InlineThinking({ group }: { group: ThinkingGroup }) {
  const { steps } = group;
  const latestActive = [...steps].reverse().find((step) => step.status === "active") ?? steps[steps.length - 1];
  const hasActive = !group.closed && steps.some((step) => step.status === "active");
  const [open, setOpen] = useState(!group.closed);

  useEffect(() => {
    setOpen(!group.closed);
  }, [group.closed, group.id]);

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

function InlineTool({ tool }: { tool: ToolActivity }) {
  const [open, setOpen] = useState(tool.status !== "complete");

  useEffect(() => {
    if (tool.status === "complete") setOpen(false);
  }, [tool.status]);

  return (
    <section className={`inline-tool ${open ? "open" : ""}`}>
      <button
        type="button"
        className="inline-tool-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span aria-hidden="true">⌁</span>
        <strong>调用工具</strong>
        <code>{tool.name}</code>
        <b>{tool.status === "complete" ? "已完成" : "运行中"}</b>
        <i aria-hidden="true">⌃</i>
      </button>
      {open && (tool.arguments || tool.result) && (
        <div className="inline-tool-detail">
          {tool.arguments && <code>{tool.arguments}</code>}
          {tool.result && <p>{tool.result}</p>}
        </div>
      )}
    </section>
  );
}

function InspectorSection({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return <section className="inspector-section"><header><h3>{title}</h3><span>{count}</span></header>{children}</section>;
}

function EmptyInspector({ text }: { text: string }) {
  return <div className="inspector-empty"><span>—</span><p>{text}</p></div>;
}

function SidebarIcon({ side }: { side: "left" | "right" }) {
  return (
    <svg className={side === "right" ? "mirrored" : ""} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="5" y="5" width="14" height="14" rx="2.5" />
      <path d="M10 6v12" />
    </svg>
  );
}

function Picker({
  label,
  items,
  selected,
  onChange
}: {
  label: string;
  items: Array<{ id: string; name: string }>;
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const pickerRef = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const closeOnOutsidePointerDown = (event: globalThis.PointerEvent) => {
      const picker = pickerRef.current;
      if (picker?.open && !picker.contains(event.target as Node)) {
        picker.removeAttribute("open");
      }
    };

    document.addEventListener("pointerdown", closeOnOutsidePointerDown);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointerDown);
  }, []);

  return (
    <details ref={pickerRef} className={`option-picker ${selected.length > 0 ? "has-selection" : ""}`}>
      <summary>{label}<span>⌃</span></summary>
      <div className="picker-popover">
        <header><strong>{label.startsWith("MCP") ? "MCP 服务" : "Skills"}</strong><small>可多选</small></header>
        {items.length === 0 && <p>暂无可用项</p>}
        {items.map((item) => {
          const isSelected = selected.includes(item.id);
          return (
            <label key={item.id} className={isSelected ? "selected" : ""}>
              <input
                type="checkbox"
                checked={isSelected}
                onChange={(event) => onChange(event.target.checked
                  ? [...selected, item.id]
                  : selected.filter((id) => id !== item.id))}
              />
              <span>{item.name}</span><i>{isSelected ? "✓" : ""}</i>
            </label>
          );
        })}
      </div>
    </details>
  );
}

const reasoningOptions = [
  { id: "none", name: "none", hint: "" },
  { id: "low", name: "low", hint: "" },
  { id: "medium", name: "medium", hint: "" },
  { id: "high", name: "high", hint: "" },
  { id: "max", name: "max", hint: "" }
] as const;

function isDeepSeekModel(model?: ModelProfile) {
  const marker = `${model?.id ?? ""} ${model?.name ?? ""} ${model?.model ?? ""} ${model?.baseUrl ?? ""}`.toLowerCase();
  return marker.includes("deepseek");
}

function reasoningOptionsForModel(model?: ModelProfile) {
  if (!model?.supportsReasoning) return [reasoningOptions[0]];
  // DeepSeek 只开放 high/max 两档思考强度，避免前端提交 low/medium 造成后端实际调用失败。
  if (isDeepSeekModel(model)) {
    return reasoningOptions.filter((option) => option.id === "none" || option.id === "high" || option.id === "max");
  }
  return reasoningOptions.filter((option) => option.id !== "max");
}

function ModelSettingsPicker({
  modelId,
  models,
  reasoningEffort,
  onModelChange,
  onReasoningChange
}: {
  modelId: string;
  models: ModelProfile[];
  reasoningEffort: ReasoningEffort;
  onModelChange: (value: string) => void;
  onReasoningChange: (value: ReasoningEffort) => void;
}) {
  const pickerRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<"model" | "reasoning" | null>(null);
  const currentModel = models.find((model) => model.id === modelId);
  const availableReasoningOptions = reasoningOptionsForModel(currentModel);
  const normalizedReasoning = availableReasoningOptions.some((option) => option.id === reasoningEffort) ? reasoningEffort : "none";
  const currentReasoning = reasoningOptions.find((option) => option.id === normalizedReasoning);
  const supportsReasoning = Boolean(currentModel?.supportsReasoning);

  useEffect(() => {
    if (normalizedReasoning !== reasoningEffort) onReasoningChange(normalizedReasoning);
  }, [normalizedReasoning, reasoningEffort, onReasoningChange]);

  useEffect(() => {
    const closeOnOutsidePointerDown = (event: globalThis.PointerEvent) => {
      const picker = pickerRef.current;
      if (open && picker && !picker.contains(event.target as Node)) {
        setOpen(false);
        setPanel(null);
      }
    };

    document.addEventListener("pointerdown", closeOnOutsidePointerDown);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointerDown);
  }, [open]);

  return (
    <div ref={pickerRef} className={`model-settings ${open ? "open" : ""}`}>
      {open && (
        <div className="model-settings-popovers">
          {panel && (
            <section className="model-options-card">
              <header>{panel === "model" ? "模型" : "推理强度"}</header>
              <div>
                {panel === "model" ? models.map((model) => (
                  <button className={model.id === modelId ? "selected" : ""} key={model.id} type="button" onClick={() => {
                    onModelChange(model.id);
                    setPanel(null);
                  }}>
                    <span>{model.name}</span><i>{model.id === modelId ? "✓" : ""}</i>
                  </button>
                )) : availableReasoningOptions.map((option) => (
                  <button className={option.id === reasoningEffort ? "selected" : ""} key={option.id} type="button" onClick={() => {
                    onReasoningChange(option.id);
                    setPanel(null);
                  }}>
                    <span><strong>{option.name}</strong>{option.hint && <small>{option.hint}</small>}</span>
                    <i>{option.id === reasoningEffort ? "✓" : ""}</i>
                  </button>
                ))}
              </div>
            </section>
          )}
          <section className="model-settings-card">
            <button className={panel === "model" ? "active" : ""} type="button" onClick={() => setPanel(panel === "model" ? null : "model")}>
              <strong>模型</strong><span>{currentModel?.name ?? "未选择"}</span><i>›</i>
            </button>
            <button
              className={panel === "reasoning" ? "active" : ""}
              type="button"
              disabled={!supportsReasoning}
              onClick={() => setPanel(panel === "reasoning" ? null : "reasoning")}
            >
              <strong>推理强度</strong><span>{supportsReasoning ? currentReasoning?.name : "不支持"}</span><i>›</i>
            </button>
          </section>
        </div>
      )}
      <button className="model-settings-bar" type="button" aria-expanded={open} onClick={() => {
        setOpen((current) => {
          const nextOpen = !current;
          setPanel(nextOpen ? "model" : null);
          return nextOpen;
        });
      }}>
        <span className="model-settings-current">
          <strong>{currentModel?.name ?? "选择模型"}</strong>
          {supportsReasoning && <em>{currentReasoning?.name}</em>}
        </span>
        <i>⌃</i>
      </button>
    </div>
  );
}
