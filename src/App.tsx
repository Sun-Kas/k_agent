import {
  FormEvent,
  KeyboardEvent,
  PointerEvent as ReactPointerEvent,
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
  SessionSummary,
  SkillConfig,
  ThinkingActivity,
  ToolActivity
} from "./types";

const suggestions = [
  "帮我拆解这个需求，并给出执行计划",
  "总结当前项目结构和关键模块",
  "检查可用工具，并说明能完成什么"
];

const createMessage = (role: ChatMessage["role"], content: string): ChatMessage => ({
  id: crypto.randomUUID(),
  role,
  content,
  createdAt: new Date().toISOString()
});

export function App() {
  const welcomeMessage = useMemo(
    () => createMessage("assistant", appConfig.initialAssistantMessage),
    []
  );
  const [messages, setMessages] = useState<ChatMessage[]>([welcomeMessage]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [trace, setTrace] = useState<string[]>([]);
  const [tasks, setTasks] = useState<string[]>([]);
  const [thinking, setThinking] = useState<ThinkingActivity[]>([]);
  const [tools, setTools] = useState<ToolActivity[]>([]);
  const [health, setHealth] = useState<HealthState | null>(null);
  const [status, setStatus] = useState<string>(appConfig.status.idle);
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
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
  const [reasoningEffort, setReasoningEffort] = useState<"none" | "low" | "medium" | "high">("none");
  const [attachments, setAttachments] = useState<Array<{ name: string; dataUrl: string; type: string }>>([]);
  const [listening, setListening] = useState(false);
  const streamEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const speechRef = useRef<{ start: () => void; stop: () => void } | null>(null);

  useEffect(() => {
    void refreshSessions();
    void refreshHealth();
    void refreshOptions();
    const savedId = localStorage.getItem(appConfig.storageKey);
    if (savedId) void openSession(savedId);
    return () => abortRef.current?.abort();
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
    streamEndRef.current?.scrollIntoView({ behavior: loading ? "smooth" : "auto" });
  }, [messages, tools, loading]);

  const filteredSessions = sessions.filter((session) =>
    session.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  );
  const activeSession = sessions.find((session) => session.id === sessionId);

  async function refreshHealth() {
    try {
      setHealth(await getHealth());
    } catch {
      setHealth(null);
    }
  }

  async function refreshSessions() {
    try {
      setSessions(await listSessions());
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
      setMessages(data.messages.length ? data.messages : [welcomeMessage]);
      setTrace(data.trace);
      setTasks(data.tasks);
      setThinking(data.thinking ?? []);
      setTools([]);
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
    setMessages([welcomeMessage]);
    setTrace([]);
    setTasks([]);
    setThinking([]);
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

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = input.trim();
    if ((!prompt && !attachments.length) || loading || !modelId) return;

    const nextMessages = [...messages, createMessage("user", prompt)];
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
        reasoningEffort,
        attachments
      }
    };

    const controller = new AbortController();
    abortRef.current = controller;
    setMessages(nextMessages);
    setInput("");
    setAttachments([]);
    setTrace([]);
    setThinking([]);
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
      setMessages((current) => [
        ...current,
        createMessage("assistant", `${appConfig.text.requestErrorPrefix}${detail}`)
      ]);
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
        setMessages((current) => [...current, createStreamingMessage(event.messageId)]);
        break;
      case "TEXT_MESSAGE_CONTENT":
        setMessages((current) => current.map((message) =>
          message.id === event.messageId
            ? { ...message, content: message.content + event.delta }
            : message
        ));
        break;
      case "TOOL_CALL_START":
        setTools((current) => [...current, {
          id: event.toolCallId,
          name: event.toolCallName,
          arguments: "",
          status: "preparing"
        }]);
        setStatus(`调用 ${event.toolCallName}`);
        break;
      case "TOOL_CALL_ARGS":
        updateTool(event.toolCallId, (tool) => ({
          ...tool, arguments: tool.arguments + event.delta, status: "running"
        }));
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
        if (event.name === "thinking") {
          const step = event.value as unknown as ThinkingActivity;
          setThinking((current) => {
            const exists = current.some((item) => item.id === step.id);
            return exists
              ? current.map((item) => item.id === step.id ? step : item)
              : [...current, step];
          });
        }
        break;
      case "STATE_SNAPSHOT":
        setMessages(event.snapshot.messages);
        setTrace(event.snapshot.trace);
        setTasks(event.snapshot.tasks);
        setThinking(event.snapshot.thinking ?? []);
        break;
      case "RUN_FINISHED":
        setStatus(appConfig.status.complete);
        break;
      case "RUN_ERROR":
        setStatus(appConfig.status.failed);
        setMessages((current) => [
          ...current,
          createMessage("assistant", `${appConfig.text.requestErrorPrefix}${event.message}`)
        ]);
        break;
    }
  }

  function updateTool(id: string, transform: (tool: ToolActivity) => ToolActivity) {
    setTools((current) => current.map((tool) => tool.id === id ? transform(tool) : tool));
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
      className={`shell ${inspectorOpen ? "" : "inspector-hidden"}`}
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
          <button className="icon-button menu-button" type="button" onClick={() => setSidebarOpen(true)} aria-label="打开会话侧栏">☰</button>
          <div className="thread-heading">
            <span className="thread-kicker">{sessionId ? "当前会话" : "新会话"}</span>
            <h1>{activeSession?.title ?? "今天想一起完成什么？"}</h1>
          </div>
          <div className="topbar-actions">
            <span className={`status-pill ${loading ? "running" : ""}`}>
              <i />{status}
            </span>
            <button className="icon-button" type="button" onClick={() => setInspectorOpen((value) => !value)} aria-label="切换运行详情">◧</button>
          </div>
        </header>

        <section className="message-stream" aria-live="polite">
          {messages.length === 1 && (
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

          {messages.length > 1 && messages.map((message) => (
            <article className={`message-row ${message.role}`} key={message.id}>
              <div className="avatar">{message.role === "user" ? "你" : "K"}</div>
              <div className="message-body">
                <header>
                  <strong>{message.role === "user" ? "你" : "K Agent"}</strong>
                  <time>{formatTime(message.createdAt)}</time>
                  {message.content && (
                    <button type="button" onClick={() => void navigator.clipboard.writeText(message.content)}>复制</button>
                  )}
                </header>
                <div className="bubble">
                  {message.content ? <MarkdownContent content={message.content} /> : <Typing />}
                </div>
              </div>
            </article>
          ))}
          <div ref={streamEndRef} />
        </section>

        <form className="composer" onSubmit={handleSubmit}>
          {attachments.length > 0 && <div className="attachment-list">{attachments.map((attachment, index) => <span key={`${attachment.name}-${index}`}>▧ {attachment.name}<button type="button" onClick={() => setAttachments(attachments.filter((_, i) => i !== index))}>×</button></span>)}</div>}
          <textarea
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
          <button type="button" onClick={() => setInspectorOpen(false)} aria-label="关闭运行详情">×</button>
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
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function formatRelativeTime(value: string) {
  const delta = Date.now() - new Date(value).getTime();
  if (delta < 60_000) return "刚刚";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  return new Date(value).toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

function thinkingIcon(phase: ThinkingActivity["phase"]) {
  if (phase === "tool") return "⌁";
  if (phase === "synthesis" || phase === "complete") return "✓";
  return "✦";
}

function Typing() {
  return <span className="typing"><i /><i /><i /></span>;
}

function InspectorSection({ title, count, children }: { title: string; count: number; children: React.ReactNode }) {
  return <section className="inspector-section"><header><h3>{title}</h3><span>{count}</span></header>{children}</section>;
}

function EmptyInspector({ text }: { text: string }) {
  return <div className="inspector-empty"><span>—</span><p>{text}</p></div>;
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
    <details ref={pickerRef} className="option-picker">
      <summary>{label}<span>⌃</span></summary>
      <div className="picker-popover">
        <header><strong>{label.startsWith("MCP") ? "MCP 服务" : "Skills"}</strong><small>可多选</small></header>
        {items.length === 0 && <p>暂无可用项</p>}
        {items.map((item) => (
          <label key={item.id}>
            <input
              type="checkbox"
              checked={selected.includes(item.id)}
              onChange={(event) => onChange(event.target.checked
                ? [...selected, item.id]
                : selected.filter((id) => id !== item.id))}
            />
            <span>{item.name}</span><i>{selected.includes(item.id) ? "✓" : ""}</i>
          </label>
        ))}
      </div>
    </details>
  );
}

const reasoningOptions = [
  { id: "none", name: "关闭", hint: "" },
  { id: "low", name: "轻度", hint: "" },
  { id: "medium", name: "中度", hint: "" },
  { id: "high", name: "高度", hint: "更深入地分析复杂问题" }
] as const;

function ModelSettingsPicker({
  modelId,
  models,
  reasoningEffort,
  onModelChange,
  onReasoningChange
}: {
  modelId: string;
  models: ModelProfile[];
  reasoningEffort: typeof reasoningOptions[number]["id"];
  onModelChange: (value: string) => void;
  onReasoningChange: (value: typeof reasoningOptions[number]["id"]) => void;
}) {
  const pickerRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [panel, setPanel] = useState<"model" | "reasoning" | null>(null);
  const currentModel = models.find((model) => model.id === modelId);
  const currentReasoning = reasoningOptions.find((option) => option.id === reasoningEffort);
  const supportsReasoning = Boolean(currentModel?.supportsReasoning);

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
                )) : reasoningOptions.map((option) => (
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
        setOpen((current) => !current);
        if (open) setPanel(null);
      }}>
        <strong>{currentModel?.name ?? "选择模型"}</strong>
        {supportsReasoning && <span>{currentReasoning?.name}</span>}
        <i>⌄</i>
      </button>
    </div>
  );
}
