import type { TerminalPageViewModel } from "./types.js";

/**
 * CLI 唯一的命令目录。
 *
 * `/` 选择栏和 Ctrl+K 命令面板都消费这一份数据，两者只是呈现方式不同；
 * 新增条目必须同时在 application 层实现，否则用户会看到一个永远返回
 * “未知命令”的入口。
 */
export interface SlashCommandItem {
  name: string;
  group: string;
  hint: string;
  /** 需要补充参数的命令用 Tab 补全后继续输入，不能直接回车执行。 */
  takesArguments?: boolean;
  /** 只读查询在运行中和断线时依然可用，不需要参与可用性判断。 */
  readOnly?: boolean;
}

export const SLASH_COMMANDS: readonly SlashCommandItem[] = [
  { name: "new", group: "会话", hint: "新建 Session（首次发送时创建）" },
  { name: "sessions", group: "会话", hint: "打开 Session 选择器", readOnly: true },
  { name: "chat", group: "会话", hint: "回到对话页面", readOnly: true },
  { name: "home", group: "导航", hint: "回到首页", readOnly: true },
  { name: "team", group: "导航", hint: "Agent Team 与任务", readOnly: true },
  { name: "auto", group: "导航", hint: "定时任务与执行记录", readOnly: true },
  { name: "doctor", group: "导航", hint: "连接与能力诊断", readOnly: true },
  { name: "workspace", group: "文件", hint: "浏览当前 Session 工作区", takesArguments: true },
  { name: "stop", group: "运行", hint: "停止当前 Run，保留已产生内容" },
  { name: "cancel", group: "运行", hint: "请求服务端取消并回滚本轮" },
  { name: "trace", group: "运行", hint: "查看当前时间线概况", readOnly: true },
  { name: "model", group: "运行时", hint: "查看当前模型（切换请用 Web 配置）", readOnly: true },
  { name: "agent", group: "运行时", hint: "查看当前 Agent 类型", readOnly: true },
  { name: "mcp", group: "运行时", hint: "查看已启用 MCP（开关请用 Web 配置）", readOnly: true },
  { name: "skill", group: "运行时", hint: "查看已启用 Skill（开关请用 Web 配置）", readOnly: true },
  { name: "permissions", group: "运行时", hint: "查看权限模式（修改请用 Web 配置）", readOnly: true },
  { name: "help", group: "其他", hint: "列出全部快捷键", readOnly: true },
  { name: "quit", group: "其他", hint: "退出终端工作台", readOnly: true },
];

/**
 * 判断当前草稿是否处于 `/` 命令选择状态。
 * 一旦出现空白就说明用户开始输入参数，此时必须收起选择栏，避免遮挡正文输入。
 */
export function slashQuery(draft: string): string | undefined {
  if (!draft.startsWith("/")) return undefined;
  const rest = draft.slice(1);
  if (/\s/.test(rest)) return undefined;
  return rest;
}

export function filterSlashCommands(query: string): SlashCommandItem[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...SLASH_COMMANDS];
  const scored = SLASH_COMMANDS
    .map((item) => ({ item, score: matchScore(item, needle) }))
    .filter((entry) => entry.score > 0);
  // 排序必须确定：先按匹配质量，再按命令名，避免同一次输入出现抖动的候选顺序。
  scored.sort((left, right) => right.score - left.score || left.item.name.localeCompare(right.item.name));
  return scored.map((entry) => entry.item);
}

export interface SlashCommandAvailability {
  enabled: boolean;
  /** 不可用时必须给出原因；命令不能因为暂时不可用就从列表里消失。 */
  reason?: string;
}

/**
 * 命令可用性只依据已投影的运行状态判断，不询问服务端。
 * 判断结果只影响能否执行，不影响是否显示。
 */
export function slashCommandAvailability(
  item: SlashCommandItem,
  model: TerminalPageViewModel,
): SlashCommandAvailability {
  if (item.readOnly) return { enabled: true };
  const running = model.timeline.runStatus === "running";
  if (item.name === "stop" || item.name === "cancel") {
    return running ? { enabled: true } : { enabled: false, reason: "没有运行中的任务" };
  }
  if (item.name === "workspace") {
    if (!model.activeSessionId) return { enabled: false, reason: "尚未打开 Session" };
    return { enabled: true };
  }
  if (item.name === "new") {
    return running ? { enabled: false, reason: "当前 Run 未结束" } : { enabled: true };
  }
  return { enabled: true };
}

/** 选择栏右侧展示的当前值，让用户在执行前就能看到运行时状态。 */
export function slashCommandValue(item: SlashCommandItem, model: TerminalPageViewModel): string {
  if (item.name === "model") return model.runtime.modelId || "未选择";
  if (item.name === "agent") return model.runtime.agentKind;
  if (item.name === "mcp") return String(model.runtime.mcpCount);
  if (item.name === "skill") return String(model.runtime.skillCount);
  if (item.name === "permissions") return model.runtime.permissionMode;
  if (item.name === "sessions") return String(model.sessions.length);
  if (item.name === "team") return String(model.teams.length);
  if (item.name === "auto") return String(model.automations.length);
  return "";
}

/** 候选行右侧的统一注解：不可用原因优先于当前值。 */
export function slashCommandAnnotation(item: SlashCommandItem, model: TerminalPageViewModel): string {
  const availability = slashCommandAvailability(item, model);
  if (!availability.enabled) return availability.reason ?? "当前不可用";
  const value = slashCommandValue(item, model);
  return value ? `${item.hint} · ${value}` : item.hint;
}

function matchScore(item: SlashCommandItem, needle: string): number {
  if (item.name === needle) return 100;
  if (item.name.startsWith(needle)) return 80;
  if (item.name.includes(needle)) return 60;
  if (item.hint.toLowerCase().includes(needle)) return 20;
  return 0;
}
