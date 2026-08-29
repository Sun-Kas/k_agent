import { createInterface } from "node:readline/promises";
import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { timelineFromSession } from "../application/event-projector.js";
import { configFromCommand, printJson } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

function clientFor(command: Command): AccessLayerClient {
  return new AccessLayerClient(configFromCommand(command).endpoint);
}

export async function listSessions(command: Command): Promise<void> {
  try {
    const sessions = await clientFor(command).listSessions();
    if (command.opts<{ json?: boolean }>().json) return printJson(sessions);
    if (!sessions.length) return void process.stdout.write("暂无 Session\n");
    process.stdout.write(`${sessions.map((session) => `${session.id}\t${session.messageCount}\t${session.updatedAt}\t${session.title}`).join("\n")}\n`);
  } catch (error) { commandFailure(error); }
}

export async function showSession(sessionId: string, command: Command): Promise<void> {
  try {
    const session = await clientFor(command).getSession(sessionId);
    if (command.opts<{ json?: boolean }>().json) return printJson(session);
    const timeline = timelineFromSession(session);
    process.stdout.write(`Session ${session.sessionId}\nMessages ${session.messages.length}\nEvents ${session.events?.length ?? 0}\nStatus ${timeline.runStatus}\nOpen interrupts ${session.openInterrupts?.length ?? 0}\n`);
  } catch (error) { commandFailure(error); }
}

export async function forkSession(sessionId: string, command: Command): Promise<void> {
  try { printJson(await clientFor(command).forkSession(sessionId)); } catch (error) { commandFailure(error); }
}

export async function deleteSession(sessionId: string, options: { yes?: boolean }, command: Command): Promise<void> {
  if (!options.yes) {
    if (!process.stdin.isTTY) {
      process.stderr.write("非交互环境删除 Session 必须显式提供 --yes。\n");
      process.exitCode = EXIT_CODES.configError;
      return;
    }
    const prompt = createInterface({ input: process.stdin, output: process.stderr });
    const answer = await prompt.question(`确认删除 Session ${sessionId}？输入 delete 继续：`);
    prompt.close();
    if (answer.trim() !== "delete") return void process.stderr.write("已取消。\n");
  }
  try {
    await clientFor(command).deleteSession(sessionId);
    process.stdout.write(`${sessionId}\n`);
  } catch (error) { commandFailure(error); }
}

function commandFailure(error: unknown): void {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = EXIT_CODES.connectionError;
}
