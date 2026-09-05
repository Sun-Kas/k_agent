#!/usr/bin/env node

import { spawn } from "node:child_process";
import { once } from "node:events";
import { closeSync, existsSync, openSync, realpathSync } from "node:fs";
import { mkdtemp, readFile } from "node:fs/promises";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const CLI_ROOT = path.resolve(path.dirname(SCRIPT_PATH), "..");
const PROJECT_ROOT = path.resolve(CLI_ROOT, "..");
const LOCAL_ENDPOINT = "http://127.0.0.1:3001";
const STARTUP_TIMEOUT_MS = boundedNumber(process.env.K_AGENT_LOCAL_START_TIMEOUT_MS, 30_000, 5_000, 120_000);

/**
 * 本脚本只是源码仓库的开发监督器，不属于发布后的 CLI Runtime。
 * 它只启动本机 Python 服务，不导入服务端模块，也不接管 Session 或审批状态。
 */
export async function runDevLocal(cliArguments = process.argv.slice(2)) {
  const python = resolvePythonExecutable(PROJECT_ROOT);
  const tsx = process.platform === "win32"
    ? path.join(CLI_ROOT, "node_modules", ".bin", "tsx.cmd")
    : path.join(CLI_ROOT, "node_modules", ".bin", "tsx");
  if (!existsSync(tsx)) throw new Error("CLI 依赖尚未安装，请先执行 npm --prefix cli install");

  const services = serviceDefinitions(python);
  const decisions = [];
  for (const service of services) decisions.push({ service, action: await inspectService(service) });

  const toStart = decisions.filter((item) => item.action === "start");
  const logDirectory = toStart.length
    ? await mkdtemp(path.join(tmpdir(), "k-agent-cli-local-"))
    : undefined;
  const owned = [];
  const logDescriptors = [];
  let cliChild;
  let shuttingDown = false;
  let receivedSignal;
  let cleanupPromise;

  const cleanup = () => {
    if (cleanupPromise) return cleanupPromise;
    shuttingDown = true;
    cleanupPromise = Promise.all([...owned].reverse().map((record) => stopOwnedProcess(record.child)))
      .then(() => undefined);
    return cleanupPromise;
  };

  const signalHandler = (signal) => {
    receivedSignal = signal;
    shuttingDown = true;
    if (cliChild && !hasExited(cliChild)) cliChild.kill(signal);
    void cleanup();
  };
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(signal, signalHandler);

  try {
    for (const { service, action } of decisions) {
      if (action === "reuse") {
        process.stderr.write(`[dev:local] 复用 ${service.label} · 127.0.0.1:${service.port}\n`);
        continue;
      }
      const logPath = path.join(logDirectory, `${service.id}.log`);
      const descriptor = openSync(logPath, "a");
      logDescriptors.push(descriptor);
      const record = spawnService(service, descriptor, logPath);
      owned.push(record);
      process.stderr.write(`[dev:local] 启动 ${service.label} · 日志 ${logPath}\n`);
    }

    await waitForReady(services, owned, STARTUP_TIMEOUT_MS);
    process.stderr.write("[dev:local] Access Layer 与 Agent Backend 已就绪，正在打开 CLI…\n");

    const npm = npmInvocation();
    cliChild = spawn(npm.command, [...npm.prefixArguments, "run", "dev", "--", ...cliArguments], {
      cwd: CLI_ROOT,
      env: { ...process.env, K_AGENT_ENDPOINT: LOCAL_ENDPOINT },
      stdio: "inherit",
    });

    for (const record of owned) {
      const handleUnexpectedExit = (code, signal) => {
        if (shuttingDown || !cliChild || hasExited(cliChild)) return;
        process.stderr.write(`[dev:local] ${record.service.label} 意外退出（${formatExit(code, signal)}），日志：${record.logPath}\n`);
        cliChild.kill("SIGTERM");
      };
      record.child.once("exit", handleUnexpectedExit);
      if (hasExited(record.child)) handleUnexpectedExit(record.child.exitCode, record.child.signalCode);
    }

    const cliExit = await childExit(cliChild);
    if (receivedSignal === "SIGINT") process.exitCode = 130;
    else if (receivedSignal) process.exitCode = 1;
    else process.exitCode = cliExit.code ?? (cliExit.signal ? 1 : 0);
  } catch (error) {
    process.stderr.write(`[dev:local] 启动失败：${errorMessage(error)}\n`);
    if (logDirectory) {
      process.stderr.write(`[dev:local] 服务日志目录：${logDirectory}\n`);
      for (const record of owned) await printLogTail(record);
    }
    process.exitCode = 1;
  } finally {
    await cleanup();
    for (const descriptor of logDescriptors) closeSync(descriptor);
    for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) process.off(signal, signalHandler);
    if (owned.length) {
      restoreTerminalAfterTui();
      process.stderr.write("[dev:local] 本次启动的本地服务已关闭。\n");
    }
  }
}

export function decideServiceAction({ healthy, portOpen }, label) {
  if (healthy) return "reuse";
  if (portOpen) {
    throw new Error(`${label} 端口已被占用，但健康接口不是预期的 K Agent 服务`);
  }
  return "start";
}

export async function stopOwnedProcess(child, graceMs = 5_000) {
  if (hasExited(child)) return;
  const exited = once(child, "exit").then(() => true);
  try {
    child.kill("SIGTERM");
  } catch {
    return;
  }
  if (hasExited(child) || await Promise.race([exited, delay(graceMs).then(() => false)])) return;
  child.kill("SIGKILL");
  await Promise.race([exited, delay(1_000)]);
}

function serviceDefinitions(python) {
  return [
    {
      id: "agent-backend",
      label: "Agent Backend",
      port: 3002,
      healthUrl: "http://127.0.0.1:3002/internal/health",
      validate: (value) => isRecord(value) && value.ok === true && value.service === "agent-backend",
      command: python,
      arguments: ["-m", "backend.run_server"],
    },
    {
      id: "access-layer",
      label: "Access Layer",
      port: 3001,
      healthUrl: `${LOCAL_ENDPOINT}/api/health`,
      validate: (value) => isRecord(value) && typeof value.ok === "boolean" && typeof value.agentBackendOk === "boolean",
      command: python,
      arguments: ["-m", "access_layer.run_server"],
    },
  ];
}

async function inspectService(service) {
  if (service.validate(await fetchJson(service.healthUrl))) {
    return decideServiceAction({ healthy: true, portOpen: true }, service.label);
  }
  const portOpen = await isPortOpen(service.port);
  if (portOpen) {
    const recovered = await waitFor(() => fetchJson(service.healthUrl).then(service.validate), 2_000);
    if (recovered) return "reuse";
  }
  return decideServiceAction({ healthy: false, portOpen }, service.label);
}

function spawnService(service, logDescriptor, logPath) {
  const record = { service, logPath, spawnError: undefined, child: undefined };
  const child = spawn(service.command, service.arguments, {
    cwd: PROJECT_ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: "1", RELOAD: "false" },
    detached: process.platform !== "win32",
    stdio: ["ignore", logDescriptor, logDescriptor],
  });
  record.child = child;
  child.once("error", (error) => { record.spawnError = error; });
  return record;
}

async function waitForReady(services, owned, timeoutMs) {
  let pauseMs = 100;
  const ready = await waitFor(async () => {
    for (const record of owned) {
      if (record.spawnError) throw record.spawnError;
      if (hasExited(record.child)) {
        throw new Error(`${record.service.label} 在健康检查完成前退出（${formatExit(record.child.exitCode, record.child.signalCode)}）`);
      }
    }
    const backend = await fetchJson(services[0].healthUrl);
    const access = await fetchJson(services[1].healthUrl);
    const allReady = services[0].validate(backend)
      && services[1].validate(access)
      && access.ok === true
      && access.agentBackendOk === true;
    pauseMs = Math.min(750, Math.round(pauseMs * 1.5));
    return allReady;
  }, timeoutMs, () => pauseMs);
  if (!ready) throw new Error(`服务在 ${timeoutMs}ms 内未就绪`);
}

async function waitFor(check, timeoutMs, interval = () => 150) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await delay(interval());
  }
  return false;
}

async function fetchJson(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(1_000) });
    if (!response.ok) return undefined;
    return await response.json();
  } catch {
    return undefined;
  }
}

function isPortOpen(port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    const finish = (open) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(500);
    socket.once("connect", () => finish(true));
    socket.once("timeout", () => finish(false));
    socket.once("error", () => finish(false));
  });
}

function resolvePythonExecutable(projectRoot) {
  const candidates = process.platform === "win32"
    ? [path.join(projectRoot, ".venv", "Scripts", "python.exe")]
    : [path.join(projectRoot, ".venv", "bin", "python")];
  const executable = candidates.find(existsSync);
  if (!executable) throw new Error("未找到项目 .venv，请先按 README 完成 Python 环境安装");
  return executable;
}

function npmInvocation() {
  if (process.env.npm_execpath) {
    return { command: process.execPath, prefixArguments: [process.env.npm_execpath] };
  }
  return { command: process.platform === "win32" ? "npm.cmd" : "npm", prefixArguments: [] };
}

function childExit(child) {
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => resolve({ code, signal }));
  });
}

async function printLogTail(record) {
  try {
    const content = await readFile(record.logPath, "utf8");
    const tail = content.trimEnd().split("\n").slice(-20).join("\n");
    if (tail) process.stderr.write(`\n--- ${record.service.label} ---\n${tail}\n`);
  } catch {
    // 日志尚未创建时，保留前面的进程错误即可。
  }
}

function hasExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

/**
 * CLI 子进程退出后真实光标可能仍停在 TUI 输入行。先送到视口底部再写关闭提示。
 */
export function restoreTerminalAfterTui(stdout = process.stdout, stdin = process.stdin) {
  if (stdout.isTTY) stdout.write("\u001b[?25h\u001b[9999B\u001b[1G\n");
  try {
    if (stdin.isTTY && stdin.isRaw) stdin.setRawMode(false);
  } catch {
    // 子进程被信号打断时 stdin 可能已经不是 raw。
  }
}

function formatExit(code, signal) {
  return signal ? `signal ${signal}` : `code ${code ?? "unknown"}`;
}

function boundedNumber(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

/**
 * 全局命令通常通过 PATH 中的符号链接进入；比较真实路径才能同时识别直接执行与链接执行。
 */
function isDirectInvocation(entryPath) {
  if (!entryPath) return false;
  try {
    return realpathSync(entryPath) === realpathSync(SCRIPT_PATH);
  } catch {
    return path.resolve(entryPath) === SCRIPT_PATH;
  }
}

if (isDirectInvocation(process.argv[1])) {
  try {
    await runDevLocal();
  } catch (error) {
    process.stderr.write(`[dev:local] 启动失败：${errorMessage(error)}\n`);
    process.exitCode = 1;
  }
}
