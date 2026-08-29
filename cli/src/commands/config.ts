import type { Command } from "commander";
import { configFromCommand, printJson } from "./shared.js";

export function showConfig(command: Command): void {
  const config = configFromCommand(command);
  // CLI 配置不含模型密钥；凭据始终只由服务端配置管理。
  printJson(config);
}
