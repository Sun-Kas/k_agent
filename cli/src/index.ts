#!/usr/bin/env node

import { Command, Option } from "commander";
import { runOnce } from "./commands/run.js";
import { doctor } from "./commands/doctor.js";
import { deleteSession, forkSession, listSessions, showSession } from "./commands/sessions.js";
import { teamCommand, teamList, teamOpen } from "./commands/teams.js";
import { automationAction, automationHistory, automationList, automationShow } from "./commands/automations.js";
import { showConfig } from "./commands/config.js";
import { EXIT_CODES } from "./output/exit-codes.js";

const program = new Command();
program
  .name("k-agent")
  .description("K Agent 终端工作台与自动化客户端")
  .version("0.1.0")
  .option("--endpoint <url>", "Access Layer 地址", process.env.K_AGENT_ENDPOINT)
  .option("--agent <kind>", "Agent 类型", process.env.K_AGENT_AGENT ?? "k_agent")
  .option("--model <id>", "模型配置 ID", process.env.K_AGENT_MODEL)
  .addOption(new Option("--reasoning <level>", "推理强度").choices(["none", "low", "medium", "high", "max"]).default("none"))
  .addOption(new Option("--permission <mode>", "权限模式").choices(["default", "full_access"]).default("default"))
  .addOption(new Option("--cli-session-mode <mode>", "外部 CLI Agent 会话模式").choices(["ephemeral", "resume"]).default("ephemeral"))
  .option("--mcp <id...>", "启用的 MCP Server ID")
  .option("--skill <id...>", "启用的 Skill ID")
  .option("--plain", "禁用颜色和动态控制");

program.action(async () => {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    program.outputHelp();
    return;
  }
  await launchChat(program, { startAt: "home" });
});

const chatCommand = program.command("chat")
  .description("打开交互式 Chat 页面")
  .option("--session <id>", "打开指定 Session")
  .action(async (options: { session?: string }) => launchChat(chatCommand, { sessionId: options.session, startAt: "chat" }));

const runCommand = program.command("run <prompt>")
  .description("执行一次非交互任务")
  .option("--session <id>", "在指定 Session 继续")
  .option("--json", "输出单个最终 JSON")
  .option("--jsonl", "按到达顺序输出事件 JSONL")
  .option("--quiet", "隐藏非错误 stderr 状态")
  .action(async (prompt: string, options: { session?: string; json?: boolean; jsonl?: boolean; quiet?: boolean }) => {
    if (options.json && options.jsonl) throw new Error("--json 与 --jsonl 不能同时使用");
    await runOnce(prompt, options, runCommand);
  });

const doctorCommand = program.command("doctor")
  .description("检查 Access Layer、Backend 与终端能力")
  .action(async () => doctor(doctorCommand));

const sessions = program.command("sessions").description("管理 Session");
const sessionsList = sessions.command("list").option("--json", "输出 JSON").action(async () => listSessions(sessionsList));
const sessionsShow = sessions.command("show <session-id>").option("--json", "输出完整 JSON").action(async (sessionId: string) => showSession(sessionId, sessionsShow));
const sessionsResume = sessions.command("resume <session-id>").description("在 TUI 中恢复 Session").action(async (sessionId: string) => launchChat(sessionsResume, { sessionId, startAt: "chat" }));
const sessionsFork = sessions.command("fork <session-id>").action(async (sessionId: string) => forkSession(sessionId, sessionsFork));
const sessionsDelete = sessions.command("delete <session-id>").option("--yes", "确认删除").action(async (sessionId: string, options: { yes?: boolean }) => deleteSession(sessionId, options, sessionsDelete));

const team = program.command("team").description("查看和控制 Agent Team");
const teamListCommand = team.command("list").action(async () => teamList(teamListCommand));
const teamOpenCommand = team.command("open <team-id>").action(async (teamId: string) => teamOpen(teamId, teamOpenCommand));
for (const action of ["pause", "resume", "cancel"] as const) {
  const command = team.command(`${action} <team-id>`).action(async (teamId: string) => teamCommand(teamId, action, command));
}

const automation = program.command("automation").description("查看和控制定时任务");
const automationListCommand = automation.command("list").action(async () => automationList(automationListCommand));
const automationShowCommand = automation.command("show <task-id>").action(async (taskId: string) => automationShow(taskId, automationShowCommand));
for (const action of ["run-now", "pause", "resume"] as const) {
  const command = automation.command(`${action} <task-id>`).action(async (taskId: string) => automationAction(taskId, action, command));
}
const automationHistoryCommand = automation.command("history <task-id>").action(async (taskId: string) => automationHistory(taskId, automationHistoryCommand));

const config = program.command("config").description("显示 CLI 有效配置（不含服务端凭据）");
config.command("show").action(() => showConfig(config));

try {
  await program.parseAsync(process.argv);
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = EXIT_CODES.configError;
}

async function launchChat(
  command: Command,
  options: { sessionId?: string | undefined; startAt?: "home" | "chat" },
): Promise<void> {
  // Ink/Chalk 在模块加载时读取 NO_COLOR，因此 --plain 必须先设置环境再动态导入页面模块。
  if (command.optsWithGlobals<{ plain?: boolean }>().plain) process.env.NO_COLOR = "1";
  const { chat } = await import("./commands/chat.js");
  await chat(command, options);
}
