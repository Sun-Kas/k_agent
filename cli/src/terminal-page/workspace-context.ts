import { spawnSync } from "node:child_process";
import { basename } from "node:path";

export interface WorkspaceContext {
  folder: string;
  cwd: string;
  branch?: string;
}

/**
 * 欢迎框用的本地工作目录。只读 git 分支名，失败就省略。
 * 不读取 `.k_agent/` 或任何服务端会话文件。
 */
export function workspaceContext(cwd = process.cwd()): WorkspaceContext {
  const folder = basename(cwd) || cwd;
  const branch = gitBranch(cwd);
  return { folder, cwd, ...(branch ? { branch } : {}) };
}

export function formatWorkspaceContext(context: WorkspaceContext): string {
  return context.branch ? `${context.folder} · ${context.branch}` : context.folder;
}

function gitBranch(cwd: string): string | undefined {
  try {
    const result = spawnSync("git", ["-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], {
      encoding: "utf8",
      timeout: 800,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const branch = result.stdout?.trim();
    if (result.status === 0 && branch && branch !== "HEAD") return branch;
  } catch {
    // 没有 git 或不在仓库里时，欢迎框只显示目录名。
  }
  return undefined;
}
