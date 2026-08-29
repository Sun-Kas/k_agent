import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { AccessLayerError } from "../client/errors.js";
import { createRunInput } from "../application/run-input.js";
import { consumeRun } from "../application/run-lifecycle.js";
import { writeHumanEvent } from "../output/human.js";
import { safeJson } from "../output/json.js";
import { EXIT_CODES } from "../output/exit-codes.js";
import { configFromCommand } from "./shared.js";

export interface RunCommandOptions {
  json?: boolean;
  jsonl?: boolean;
  quiet?: boolean;
  session?: string;
}

export async function runOnce(prompt: string, options: RunCommandOptions, command: Command): Promise<void> {
  let config = configFromCommand(command);
  const client = new AccessLayerClient(config.endpoint);
  try {
    if (!config.modelId && config.agentKind === "k_agent") {
      const model = (await client.models()).find((item) => item.enabled);
      if (!model) throw new Error("Access Layer 没有可用模型，请先在 Web 配置中启用模型");
      config = { ...config, modelId: model.id };
    }
    const run = createRunInput(prompt, config, options.session);
    const controller = new AbortController();
    let userInterrupted = false;

    // SIGINT 先请求 stop，再断开本地 SSE；它与 cancel/回滚保持永久区别。
    const onSigint = (): void => {
      userInterrupted = true;
      void client.stopRun(run.sessionId, run.runId).finally(() => controller.abort());
    };
    process.once("SIGINT", onSigint);
    let result;
    try {
      result = await consumeRun(client, run.input, {
        signal: controller.signal,
        onEvent: (event) => {
          if (options.jsonl) process.stdout.write(`${safeJson(event)}\n`);
          else if (!options.json) writeHumanEvent(event, Boolean(options.quiet));
        },
      });
    } catch (error) {
      if (userInterrupted && controller.signal.aborted) {
        process.exitCode = EXIT_CODES.interrupted;
        return;
      }
      throw error;
    } finally {
      process.removeListener("SIGINT", onSigint);
    }

    if (options.json) {
      process.stdout.write(`${safeJson({ sessionId: run.sessionId, runId: run.runId, text: result.text, interrupted: result.interrupted, failed: result.failed, events: result.events })}\n`);
    } else if (!options.jsonl && result.text && !result.text.endsWith("\n")) process.stdout.write("\n");
    if (result.interrupted) {
      process.stderr.write("运行正在等待审批或用户回答；请使用 k-agent chat 恢复该 Session。\n");
      process.exitCode = EXIT_CODES.inputRequired;
    } else if (result.failed) process.exitCode = EXIT_CODES.agentError;
  } catch (error) {
    const code = error instanceof AccessLayerError ? EXIT_CODES.connectionError : EXIT_CODES.configError;
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = code;
  }
}
