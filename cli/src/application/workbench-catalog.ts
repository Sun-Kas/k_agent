/**
 * Web/CLI 功能对照的可执行目录。表单字段分为路径、查询和业务请求体，
 * 页面不拼 URL、不访问本地服务端目录；所有行为仍由 Access Layer 校验。
 */
export interface WorkbenchField {
  key: string;
  label: string;
  location: "path" | "query" | "body" | "local";
  value?: string;
  kind?: "json" | "boolean" | "number" | "secret";
  optional?: boolean;
  choices?: string[];
}
export interface WorkbenchOperation {
  id: string;
  group: string;
  title: string;
  path: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  fields: WorkbenchField[];
  opensSession?: boolean;
  /** 集合配置必须先读取再编辑，保留 Web 未暴露的字段和已配置密钥。 */
  editSource?: string;
  editKey?: string;
  local?: "import" | "runtime" | "attachment" | "copy" | "pin" | "shell" | "timeline" | "preferences";
}
const path = (key: string, label: string): WorkbenchField => ({ key, label, location: "path" });
const query = (key: string, label: string, value = "", optional = true): WorkbenchField => ({ key, label, value, optional, location: "query" });
const body = (key: string, label: string, value = "", kind?: WorkbenchField["kind"], optional = false, choices?: string[]): WorkbenchField => ({ key, label, value, optional, location: "body", ...(kind ? { kind } : {}), ...(choices ? { choices } : {}) });
const sid = path("sessionId", "会话 ID（默认当前会话）");
const pid = path("projectId", "项目 ID");
const tid = path("teamId", "团队 ID");
const aid = path("taskId", "定时任务 ID");
const rid = path("runId", "执行 ID");
const fpath = query("path", "文件路径", "", false);
const runtime = [body("agentKind", "Agent", "k_agent", undefined, false, ["k_agent", "codex", "claude_code"]), body("modelId", "模型 ID"), body("reasoningEffort", "思考强度", "none", undefined, false, ["none", "low", "medium", "high", "max"]), body("mcpServerIds", "MCP 本轮选择（空数组不选择任何能力）", "[]", "json"), body("skillIds", "Skill 选择（空数组表示全部已启用）", "[]", "json"), body("permissionMode", "权限", "default", undefined, false, ["default", "full_access"])];
const schedule = [body("name", "名称"), body("prompt", "执行内容"), body("scheduleKind", "周期", "daily", undefined, false, ["once", "daily", "weekly"]), body("localDate", "日期 YYYY-MM-DD", "", undefined, true), body("weekdays", "星期（0-6）", "[]", "json"), body("localTime", "时间 HH:mm", "09:00"), body("timezone", "时区", "Asia/Shanghai"), ...runtime];
const approval = [body("action", "决定", "approve", undefined, false, ["approve", "deny", "cancel", "answer"]), body("scope", "批准范围", "once", undefined, false, ["once", "run"]), body("answers", "问题答案 {问题ID:{selected:[],custom:''}}", "{}", "json")];
const ops: WorkbenchOperation[] = [];
function add(id: string, group: string, title: string, route: string, method: WorkbenchOperation["method"] = "GET", fields: WorkbenchField[] = [], extra: Partial<WorkbenchOperation> = {}): void {
  ops.push({ id, group, title, path: route, method, fields, ...extra });
}
add("projects", "项目", "项目列表与归属、分支、路径", "/api/projects");
add("project-create", "项目", "注册项目目录", "/api/projects", "POST", [body("rootPath", "目录绝对路径"), body("name", "项目名称", "", undefined, true)]);
add("directory", "项目", "浏览目录", "/api/fs/browse", "GET", [query("path", "目录绝对路径")]);
add("mkdir", "项目", "创建子目录", "/api/fs/mkdir", "POST", [body("parentPath", "父目录"), body("name", "子目录名称")]);
add("project-edit", "项目", "修改名称与目标分支", "/api/projects/:projectId", "PATCH", [pid, body("name", "项目名称"), body("targetBranch", "目标分支（留空不修改）", "", undefined, true)]);
add("project-clear-target", "项目", "清除目标分支", "/api/projects/:projectId", "PATCH", [pid, body("targetBranch", "目标分支", "null", "json")]);
add("project-delete", "项目", "删除项目注册", "/api/projects/:projectId", "DELETE", [pid]);
add("repository", "项目", "查看仓库分支", "/api/projects/:projectId/repository", "GET", [pid]);
add("project-session", "项目", "创建项目会话 / Local 或 Worktree", "/api/projects/:projectId/sessions", "POST", [pid, body("environment", "执行环境", "auto", undefined, false, ["auto", "local", "worktree"]), body("startingState", "起点 default 或 working-tree", '{"type":"default"}', "json")], { opensSession: true });
add("bind", "项目", "将会话迁入项目", "/api/sessions/:sessionId/project", "POST", [sid, body("projectId", "项目 ID")]);
add("pin", "项目", "置顶 / 取消置顶项目（CLI 本地）", "", "POST", [body("projectId", "项目 ID")], { local: "pin" });
add("sessions", "会话", "全部会话、归属、环境与更新时间", "/api/sessions");
add("session", "会话", "完整会话 / 能力 / 原始事件 / 审批", "/api/sessions/:sessionId", "GET", [sid]);
add("session-open", "会话", "打开会话", "/api/sessions/:sessionId", "GET", [sid], { opensSession: true });
add("session-delete", "会话", "删除会话", "/api/sessions/:sessionId", "DELETE", [sid]);
add("fork", "会话", "从消息或轮次分叉 / 同目录或独立 Worktree", "/api/sessions/:sessionId/fork", "POST", [sid, body("throughMessageId", "截止消息 ID", "", undefined, true), body("throughRunId", "截止轮次 ID", "", undefined, true), body("environment", "分叉环境", "same-directory", undefined, false, ["same-directory", "worktree"])], { opensSession: true });
add("context", "会话", "上下文容量、压缩指标与告警", "/api/sessions/:sessionId/context", "GET", [sid]);
add("compact", "会话", "压缩上下文 / 重置自动压缩", "/api/sessions/:sessionId/compact", "POST", [sid, body("instructions", "压缩要求", "", undefined, true), body("reset", "重置压缩", "false", "boolean")]);
add("environment", "工作区", "会话实际执行环境", "/api/sessions/:sessionId/environment", "GET", [sid]);
add("handoff", "工作区", "切换 Local / Worktree", "/api/sessions/:sessionId/handoff", "POST", [sid, body("target", "目标环境", "worktree", undefined, false, ["local", "worktree"])]);
add("changes", "工作区", "基于会话起点的变更列表", "/api/sessions/:sessionId/changes", "GET", [sid]);
add("diff", "工作区", "查看文件 Diff", "/api/sessions/:sessionId/diff", "GET", [sid, fpath]);
add("files", "工作区", "工作区文件列表", "/api/sessions/:sessionId/workspace", "GET", [sid]);
add("file", "工作区", "文件全文 / 二进制及截断状态", "/api/sessions/:sessionId/workspace/file", "GET", [sid, fpath]);
add("commit", "整合", "提交会话工作区", "/api/sessions/:sessionId/git/commit", "POST", [sid, body("message", "提交信息")]);
add("integration", "整合", "合入状态 / 冲突 / 目标分支", "/api/sessions/:sessionId/integration", "GET", [sid]);
for (const [id, title, suffix] of [["start", "合入目标分支", ""], ["continue", "继续合入", "/continue"], ["abort", "中止合入", "/abort"]]) add(`integration-${id}`, "整合", title!, `/api/sessions/:sessionId/integration${suffix}`, "POST", [sid]);
add("conflict", "整合", "读取冲突文件和版本 Hash", "/api/sessions/:sessionId/integration/file", "GET", [sid, fpath]);
add("resolve", "整合", "解决冲突（校验 expectedHash）", "/api/sessions/:sessionId/integration/file", "PUT", [sid, body("path", "冲突路径"), body("expectedHash", "读取时的 Hash"), body("choice", "处理方式", "manual", undefined, false, ["manual", "ours", "theirs", "delete"]), body("content", "手动解决后的内容", "", undefined, true)]);
for (const [key, title] of [["models", "模型"], ["mcp", "MCP"], ["skills", "Skill"]]) {
  add(`config-${key}`, "配置", `${title} 完整配置与来源`, `/api/config/${key}`);
  const field = key === "mcp" ? "servers" : key!;
  add(`save-${key}`, "配置", `编辑 ${title}（新增 / 修改 / 删除）`, `/api/config/${key}`, "PUT", [body(field, `${title} 配置数组（先读取当前值）`, "[]", "json")], { editSource: `/api/config/${key}`, editKey: field });
}
add("import-skill", "配置", "从本地 ZIP 导入 Skill", "", "POST", [body("file", "ZIP 文件路径")], { local: "import" });
add("mcp-tools", "配置", "MCP 工具与资源能力", "/api/mcp/capabilities");
add("mcp-reload", "配置", "重新连接 MCP", "/api/mcp/reload", "POST");
add("agents", "配置", "Agent 可用性 / 版本 / 模型 / 恢复能力", "/api/agents");
add("runtime", "运行", "设置本轮能力、网络、思考与恢复选项", "", "POST", [...runtime, body("networkAccess", "网络访问（留空跟随默认）", "", "boolean", true), body("cliSessionMode", "CLI 恢复模式", "ephemeral", undefined, false, ["ephemeral", "resume"]), body("resumeSessionId", "外部 Agent 恢复 ID", "", undefined, true), body("claudeAutoApproveTools", "Claude 自动批准工具", "[]", "json")], { local: "runtime" });
add("attach", "运行", "添加图片或视频（最多 4 个，每个 20MB）", "", "POST", [body("file", "图片 / 视频绝对路径")], { local: "attachment" });
add("attachments-clear", "运行", "清除待发送附件", "", "POST", [], { local: "attachment" });
add("timeline", "运行", "完整时间线：消息 ID / 思考 / 工具参数与结果", "", "GET", [], { local: "timeline" });
add("copy", "运行", "复制消息 / 工具结果到剪贴板", "", "POST", [body("messageId", "时间线条目 ID（留空为最后回复）", "", undefined, true)], { local: "copy" });
add("shell", "工作区", "进入会话 PTY（Ctrl+] 返回，可多标签 ID）", "", "POST", [sid, body("terminalId", "终端标签 ID", "cli")], { local: "shell" });
for (const [kind, title] of [["mcp", "MCP"], ["skills", "Skill"]]) {
  add(`market-${kind}`, "广场", `搜索 ${title}（分页）`, `/api/marketplace/${kind}`, "GET", [query("q", "关键词"), query("page", "页码", "1"), query("pageSize", "每页数量", "24")]);
  const fields = kind === "mcp" ? [query("sourceId", "来源 ID", "", false)] : [path("slug", "Skill slug")];
  const route = kind === "mcp" ? "/api/marketplace/mcp/item" : "/api/marketplace/skills/:slug";
  add(`preview-${kind}`, "广场", `预览 ${title} 安装、冲突与所需配置`, `${route}/preview`, "POST", fields);
  add(`install-${kind}`, "广场", `安装 ${title}`, `${route}/install`, "POST", [...fields, ...(kind === "mcp" ? [body("id", "本地 ID", "", undefined, true), body("env", "环境变量", "{}", "json"), body("headers", "请求头", "{}", "json")] : [body("enabled", "启用", "true", "boolean")])]);
}
add("teams", "团队", "团队列表", "/api/teams");
add("team-create", "团队", "创建团队与成员", "/api/teams", "POST", [body("name", "名称"), body("goal", "目标"), body("workspaceDir", "工作目录", "", undefined, true), body("projectId", "项目 ID（与工作目录二选一）", "", undefined, true), body("mode", "模式", "auto", undefined, false, ["auto", "manual"]), body("maxParallel", "并行数", "4", "number"), body("permissionMode", "权限", "default", undefined, false, ["default", "full_access"]), body("agents", "成员 / 职责 / 模型 / 能力", '[{"name":"主管","role":"supervisor","agentKind":"k_agent","modelId":"","isSupervisor":true,"responsibility":"","mcpServerIds":[],"skillIds":[]}]', "json")]);
add("team-detail", "团队", "完整团队看板 / 任务 / 成员 / 消息 / 产物 / 审批", "/api/teams/:teamId", "GET", [tid]);
add("team-events", "团队", "团队增量事件与任务追踪", "/api/teams/:teamId/events", "GET", [tid, query("afterSeq", "起始序号", "0"), query("taskId", "仅指定任务 ID"), query("limit", "条数", "2000")]);
add("team-command", "团队", "暂停 / 恢复 / 取消团队", "/api/teams/:teamId/commands", "POST", [tid, body("command", "命令", "pause", undefined, false, ["pause", "resume", "cancel"])]);
add("team-message", "团队", "给成员发送消息", "/api/teams/:teamId/messages", "POST", [tid, body("recipientId", "收件成员 ID"), body("senderId", "发送者", "user"), body("messageType", "消息类型", "user_message"), body("content", "内容")]);
add("team-approval", "团队", "处理团队审批 / 用户问题", "/api/teams/:teamId/approvals/:approvalId/resume", "POST", [tid, path("approvalId", "审批 ID"), ...approval]);
add("team-files", "团队", "团队产物文件列表", "/api/teams/:teamId/workspace", "GET", [tid]);
add("team-file", "团队", "读取团队产物文件", "/api/teams/:teamId/workspace/file", "GET", [tid, fpath]);
add("automations", "定时", "任务列表 / 下次执行 / 最近结果", "/api/scheduled-tasks");
add("auto-create", "定时", "创建定时任务", "/api/scheduled-tasks", "POST", schedule);
add("auto-detail", "定时", "定时任务完整配置", "/api/scheduled-tasks/:taskId", "GET", [aid]);
add("auto-edit", "定时", "编辑定时任务", "/api/scheduled-tasks/:taskId", "PUT", [aid, ...schedule], { editSource: "/api/scheduled-tasks/:taskId" });
add("auto-delete", "定时", "删除定时任务", "/api/scheduled-tasks/:taskId", "DELETE", [aid]);
for (const [action, title] of [["pause", "暂停"], ["resume", "恢复"], ["run-now", "立即执行"]]) add(`auto-${action}`, "定时", `${title}定时任务`, `/api/scheduled-tasks/:taskId/${action}`, "POST", [aid]);
add("auto-runs", "定时", "执行历史 / 错误 / 时间", "/api/scheduled-tasks/:taskId/runs", "GET", [aid]);
add("auto-session", "定时", "执行对话与完整事件", "/api/scheduled-tasks/:taskId/runs/:runId/session", "GET", [aid, rid]);
add("auto-approval", "定时", "处理定时执行审批 / 用户问题", "/api/scheduled-tasks/:taskId/runs/:runId/resume", "POST", [aid, rid, body("interruptId", "审批 ID"), ...approval]);
add("preferences", "偏好", "界面主题、阅读缩放与面板显示", "", "POST", [body("theme", "主题", "blueprint", undefined, false, ["snow", "ink", "blueprint", "forest", "ocean", "sand", "dusk"]), body("fontSizeDelta", "阅读缩放（终端以行距适配，字体由宿主管理）", "0", "number"), body("sidebar", "显示会话侧栏", "false", "boolean"), body("inspector", "展开活动检查器", "false", "boolean")], { local: "preferences" });
add("health", "诊断", "服务 / 沙箱可用性与安装建议", "/api/health");
add("catalog", "诊断", "能力目录与来源", "/api/catalog");
export const WORKBENCH_OPERATIONS: readonly WorkbenchOperation[] = ops;

export function operationRequest(op: WorkbenchOperation, values: Record<string, string>): { path: string; body?: Record<string, unknown> } {
  let route = op.path;
  const params = new URLSearchParams();
  const payload: Record<string, unknown> = {};
  for (const field of op.fields) {
    const raw = values[field.key] ?? field.value ?? "";
    if (!raw.trim() && field.optional) continue;
    if (!raw.trim()) throw new Error(`请填写${field.label}`);
    let value: unknown = raw;
    if (field.kind === "json") {
      try { value = JSON.parse(raw); } catch { throw new Error(`${field.label}必须是合法 JSON`); }
    }
    if (["mcpServerIds", "skillIds", "claudeAutoApproveTools"].includes(field.key) && (!Array.isArray(value) || !value.every((item) => typeof item === "string"))) throw new Error(`${field.label}必须是字符串数组`);
    if (field.kind === "boolean") {
      if (raw !== "true" && raw !== "false") throw new Error(`${field.label}必须是 true 或 false`);
      value = raw === "true";
    }
    if (field.kind === "number") {
      value = Number(raw);
      if (!Number.isFinite(value)) throw new Error(`${field.label}必须是数字`);
    }
    if (field.choices && !field.choices.includes(raw)) throw new Error(`${field.label}请选择 ${field.choices.join(" / ")}`);
    if (field.location === "path") route = route.replace(`:${field.key}`, encodeURIComponent(raw));
    if (field.location === "query") params.set(field.key, raw);
    if (field.location === "body") payload[field.key] = value;
  }
  return { path: route + (params.size ? `?${params}` : ""), ...(Object.keys(payload).length ? { body: payload } : {}) };
}

export function initialOperationValues(op: WorkbenchOperation, sessionId?: string): Record<string, string> {
  return Object.fromEntries(op.fields.map((field) => [field.key, field.key === "sessionId" ? sessionId ?? "" : field.value ?? ""]));
}
