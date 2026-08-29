import React from "react";
import { render } from "ink";
import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { ChatController } from "../application/chat-controller.js";
import { restoreTerminalAfterTui } from "../output/restore-terminal.js";
import { configFromCommand } from "./shared.js";

export async function chat(command: Command, options: { sessionId?: string | undefined; startAt?: "home" | "chat" } = {}): Promise<void> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    throw new Error("交互式终端页面需要 TTY；脚本中请使用 k-agent run");
  }
  const config = configFromCommand(command);
  const app = render(
    <ChatController client={new AccessLayerClient(config.endpoint)} initialConfig={config} initialSessionId={options.sessionId} startAt={options.startAt ?? "chat"} />,
    { exitOnCtrlC: false },
  );
  await app.waitUntilExit();
  restoreTerminalAfterTui();
}
