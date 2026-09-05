import type { Command } from "commander";
import { AccessLayerClient } from "../client/access-layer-client.js";
import { choicesFromModels, findChoice } from "../application/runtime-switch.js";
import { configFromCommand } from "./shared.js";
import { EXIT_CODES } from "../output/exit-codes.js";

export async function modelList(command: Command): Promise<void> {
  const client = new AccessLayerClient(configFromCommand(command).endpoint);
  try {
    const models = choicesFromModels(await client.models());
    if (models.length === 0) {
      process.stdout.write("当前没有配置模型。\n");
      return;
    }
    for (const model of models) {
      const flag = model.enabled ? "on " : "off";
      const name = model.name === model.id ? model.id : `${model.name} (${model.id})`;
      const note = model.note ? ` · ${model.note}` : "";
      process.stdout.write(`${flag} ${name}${note}\n`);
    }
  } catch (error) {
    fail(error);
  }
}

export async function modelShow(command: Command): Promise<void> {
  const config = configFromCommand(command);
  const client = new AccessLayerClient(config.endpoint);
  try {
    const models = choicesFromModels(await client.models());
    const current = config.modelId
      ? findChoice(models, config.modelId)
      : models.find((item) => item.enabled);
    process.stdout.write(`${current?.id ?? config.modelId ?? "未选择"}\n`);
  } catch (error) {
    fail(error);
  }
}

function fail(error: unknown): void {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = EXIT_CODES.connectionError;
}
