import { readFile, stat, mkdir, writeFile } from "node:fs/promises";
import { basename, extname, join } from "node:path";
import { homedir } from "node:os";
import { spawn } from "node:child_process";
import type { MediaAttachment } from "../protocol/index.js";

/** 附件属于 CLI 用户显式选择的本地文件；执行 cwd 仍由服务端会话环境决定。 */
export async function readAttachment(file: string): Promise<MediaAttachment> {
  const types: Record<string, string> = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime" };
  const type = types[extname(file).toLowerCase()];
  if (!type) throw new Error("请选择支持的图片或视频文件");
  if ((await stat(file)).size > 20 * 1024 * 1024) throw new Error("单个附件不能超过 20MB");
  const bytes = await readFile(file);
  return { name: basename(file), type, dataUrl: `data:${type};base64,${bytes.toString("base64")}` };
}

export async function copyText(text: string): Promise<void> {
  const program = process.platform === "darwin" ? "pbcopy" : process.platform === "win32" ? "clip" : "xclip";
  const args = program === "xclip" ? ["-selection", "clipboard"] : [];
  await new Promise<void>((resolve, reject) => {
    // 内容走 stdin，不经过 shell，消息里的命令字符不能在宿主机执行。
    const child = spawn(program, args, { stdio: ["pipe", "ignore", "pipe"] });
    let detail = "";
    child.stderr.on("data", (data: Buffer) => { detail += data.toString(); });
    child.on("error", () => reject(new Error(`剪贴板不可用，请安装或启用 ${program}`)));
    child.on("close", (code) => code === 0 ? resolve() : reject(new Error(detail || "复制失败")));
    child.stdin.on("error", () => {});
    child.stdin.end(text);
  });
}

export async function readPins(): Promise<string[]> {
  try {
    const parsed: unknown = JSON.parse(await readFile(join(homedir(), ".config", "k-agent-cli", "pins.json"), "utf8"));
    return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : [];
  } catch (error) {
    if (error instanceof SyntaxError || (error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}

export async function togglePin(projectId: string): Promise<string[]> {
  const dir = join(homedir(), ".config", "k-agent-cli");
  const file = join(dir, "pins.json");
  let pins = await readPins();
  pins = pins.includes(projectId) ? pins.filter((id) => id !== projectId) : [...pins, projectId];
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await writeFile(file, JSON.stringify(pins), { mode: 0o600 });
  return pins;
}
