const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();

export const appConfig = {
  // Development keeps Vite and Access Layer on separate loopback ports. A
  // built client is served by Access Layer itself and therefore stays same-origin.
  apiBaseUrl: configuredApiBaseUrl ?? (import.meta.env.DEV ? "http://localhost:3001" : ""),
  storageKey: import.meta.env.VITE_SESSION_STORAGE_KEY ?? "k-agent-session-id",
  agUiEndpoint: "/api/agent",
  sseMessageDelimiter: "\n\n",
  sseDataPrefix: "data: ",
  status: {
    idle: "空闲",
    preparing: "准备请求 agent",
    processing: "处理中",
    complete: "完成",
    failed: "失败"
  },
  text: {
    newSession: "新建会话",
    heroEyebrow: "React Agent Starter",
    heroTitle: "可扩展的 OpenAI Agent 框架",
    heroCopy:
      "前端负责聊天体验，后端负责模型调用、工具执行、会话管理与 MCP 集成，方便你继续往业务 agent 演进。",
    composerPlaceholder: "输入你的任务，例如：总结今天待办，或调用工具查询信息",
    loadingButton: "思考中...",
    sendButton: "发送",
    traceTitle: "执行轨迹",
    taskTitle: "任务拆解",
    sessionTitle: "会话历史",
    emptyTrace: "当前还没有工具执行记录。",
    emptyTasks: "当前还没有提取到任务。",
    emptySessions: "当前还没有历史会话。",
    statusLabel: "状态：",
    requestErrorPrefix: "请求失败：",
    messageCountSuffix: " 条消息"
  }
} as const;
