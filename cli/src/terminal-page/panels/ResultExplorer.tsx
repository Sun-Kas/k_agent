import React, { useMemo, useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";
import { previewValue } from "./StructuredEditor.js";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { presentWorkbenchResult } from "../../application/workbench-presenter.js";
import stringWidth from "string-width";

const labels: Record<string, string> = { name: "名称", goal: "目标", status: "状态", conversation: "工作对话", arguments: "工具参数", result: "工具结果", liveOutput: "实时输出", kind: "类型", thinking: "思考", tool: "工具", approval: "审批", runStatus: "运行状态", agents: "成员", tasks: "任务看板", artifacts: "产物", mailbox: "成员消息", conflicts: "冲突文件", files: "文件", events: "事件追踪", messages: "对话", openInterrupts: "待处理审批", environment: "执行环境", capabilities: "本轮能力", rootPath: "项目目录", workspaceDir: "工作目录", nextRunAt: "下次执行", latestRun: "最近执行", errorMessage: "失败原因", prompt: "执行内容", localTime: "执行时间", timezone: "时区", weekdays: "执行星期", updatedAt: "更新时间", title: "标题", content: "正文", diff: "差异", path: "路径", binary: "二进制", truncated: "已截断", installed: "已安装", installPreview: "安装配置与限制", warnings: "警告", page: "分页", items: "搜索结果", sourceStatus: "来源状态", pinnedProjectIds: "置顶项目", projects: "项目列表", description: "说明", ownerAgentId: "负责人", dependsOn: "前置任务", attempt: "尝试次数", maxAttempts: "最大尝试", responsibility: "职责", version: "版本", sha256: "内容 Hash", permissionMode: "权限", supervisorState: "主管调度状态" };
export interface ExplorerNavigation { operationId: string; values: Record<string, string>; execute?: boolean }

/** 业务对象按成员、任务、消息、文件等分层浏览；动作从当前实体继承 ID，不让用户抄 ID。 */
export function ResultExplorer({ operationId, data, context, onNavigate, onRefresh, onBack }: { operationId: string; data: unknown; context: Record<string, string>; onNavigate: (navigation: ExplorerNavigation) => void; onRefresh: () => void; onBack: () => void }): React.ReactElement {
  const { stdout } = useStdout();
  data = useMemo(() => presentWorkbenchResult(operationId, data), [operationId, data]);
  const [trail, setTrail] = useState<string[]>(() => {
    if (data && typeof data === "object" && !Array.isArray(data)) {
      const record = data as Record<string, unknown>;
      if (Array.isArray(record.conversation)) return ["conversation"];
      if (typeof record.diff === "string") return ["diff"];
      if (typeof record.content === "string") return ["content"];
      if (Array.isArray(record.items)) return ["items"];
      if (Array.isArray(record.files)) return ["files"];
      if (Array.isArray(record.projects)) return ["projects"];
    }
    return [];
  });
  const [selected, setSelected] = useState(0);
  const [offset, setOffset] = useState(0);
  const current = trail.reduce<unknown>((value, key) => (value as Record<string, unknown>)?.[key], data);
  const entries = current && typeof current === "object" ? Object.entries(current) : [];
  const picked = entries[Math.min(selected, Math.max(0, entries.length - 1))];
  const height = Math.max(3, (stdout.rows ?? 24) - 10);
  const record = current && typeof current === "object" && !Array.isArray(current) ? current as Record<string, unknown> : undefined;
  const scalar = useMemo(() => wrapText(typeof current === "string" ? current : current === undefined ? "暂无数据" : String(current), Math.max(10, (stdout.columns ?? 80) - 4)), [current, stdout.columns]);
  const selectedRecord = picked?.[1] && typeof picked[1] === "object" && !Array.isArray(picked[1]) ? picked[1] as Record<string, unknown> : record;
  const entity = (Array.isArray(current) ? selectedRecord : record) ?? {};
  function go(id: string, values: Record<string, string>, execute = false): void { onNavigate({ operationId: id, values: { ...context, ...values }, execute }); }
  const entityId = String(entity.id ?? "");
  useInput((input, key) => {
    if (key.escape || key.leftArrow) {
      if (trail.length) { setTrail(trail.slice(0, -1)); setSelected(0); setOffset(0); } else onBack();
      return;
    }
    if (input === "r") return onRefresh();
    if (key.upArrow || key.downArrow) {
      const delta = key.downArrow ? 1 : -1;
      if (current && typeof current === "object") setSelected((n) => (n + entries.length + delta) % Math.max(1, entries.length));
      else setOffset((n) => Math.max(0, Math.min(Math.max(0, scalar.length - height), n + delta)));
      return;
    }
    if (key.pageUp || key.pageDown) { setOffset((n) => Math.max(0, Math.min(Math.max(0, scalar.length - height), n + (key.pageDown ? height : -height)))); return; }
    const projectId = context.projectId || (operationId.startsWith("project") || operationId === "projects" ? entityId : "");
    const teamId = context.teamId || (operationId === "teams" ? entityId : "");
    const taskId = context.taskId || (operationId === "automations" ? entityId : "");
    const sessionId = context.sessionId || (operationId === "sessions" ? entityId : "");
    if (projectId) {
      if (input === "n") return go("project-session", { projectId });
      if (input === "e") return go("project-edit", { projectId, name: String(entity.name ?? ""), targetBranch: String(entity.targetBranch ?? "") });
      if (input === "g") return go("repository", { projectId }, true);
      if (input === "p") return go("pin", { projectId });
      if (input === "d") return go("project-delete", { projectId });
    }
    if (teamId) {
      if (input === "m") return go("team-message", { teamId, recipientId: entityId });
      if (input === "c") return go("team-command", { teamId });
      if (input === "f") return go("team-files", { teamId }, true);
      if (input === "t") return go("team-events", { teamId, ...(entity.teamId && entityId ? { taskId: entityId } : {}) }, true);
      if (input === "a") return go("team-approval", { teamId, approvalId: String(entity.interruptId ?? entity.id ?? "") });
    }
    if (taskId && operationId !== "team-events") {
      if (input === "e") return go("auto-edit", { taskId });
      if (input === "h") return go("auto-runs", { taskId }, true);
      if (input === "c") return go("auto-pause", { taskId });
      if (input === "u") return go("auto-resume", { taskId });
      if (input === "n") return go("auto-run-now", { taskId });
      if (input === "d") return go("auto-delete", { taskId });
      if (input === "a") return go("auto-approval", { taskId, runId: context.runId || entityId, interruptId: String(entity.interruptId ?? entity.id ?? "") });
    }
    if (sessionId && operationId === "conflict" && input === "x") return go("resolve", { sessionId, path: String((data as Record<string, unknown>)?.path ?? ""), expectedHash: String((data as Record<string, unknown>)?.hash ?? ""), content: String((data as Record<string, unknown>)?.content ?? "") });
    if (sessionId && operationId === "integration") {
      if (input === "s") return go("integration-start", { sessionId });
      if (input === "u") return go("integration-continue", { sessionId });
      if (input === "x") return go("integration-abort", { sessionId });
      if (input === "m") return go("commit", { sessionId });
    }
    if (sessionId) {
      if (input === "o") return go("session-open", { sessionId }, true);
      if (input === "b") return go("fork", { sessionId, throughMessageId: String(entity.messageId ?? (entity.kind === "user" || entity.kind === "text" || entity.role ? entityId : "")), throughRunId: String(entity.runId ?? "") });
      if (input === "c") return go("changes", { sessionId }, true);
      if (input === "f") return go("files", { sessionId }, true);
      if (input === "h") return go("handoff", { sessionId });
      if (input === "d") return go("session-delete", { sessionId });
    }
    if (input === "i" && (operationId.includes("mcp") || operationId.includes("skills"))) {
      const kind = operationId.includes("mcp") ? "mcp" : "skills";
      return go(`install-${kind}`, kind === "mcp" ? { sourceId: String(entity.sourceId ?? context.sourceId ?? "") } : { slug: String(entity.sourceId ?? context.slug ?? "") });
    }
    if ((input === "n" || input === "p") && operationId.startsWith("market-")) return go(operationId, { page: String(Math.max(1, Number(context.page || 1) + (input === "n" ? 1 : -1))) }, true);
    if (!key.return || !picked) return;
    const value = picked[1];
    const item = value && typeof value === "object" ? value as Record<string, unknown> : undefined;
    if (Array.isArray(current) && typeof value === "string" && operationId === "integration" && trail.at(-1) === "conflicts") return go("conflict", { path: value }, true);
    if (Array.isArray(current) && item) {
      if (operationId === "teams") return go("team-detail", { teamId: String(item.id) }, true);
      if (operationId === "automations") return go("auto-detail", { taskId: String(item.id) }, true);
      if (operationId === "sessions") return go("session", { sessionId: String(item.id) }, true);
      if (operationId.startsWith("market-")) return go(operationId === "market-mcp" ? "preview-mcp" : "preview-skills", operationId === "market-mcp" ? { sourceId: String(item.sourceId) } : { slug: String(item.sourceId) }, true);
      if (operationId === "auto-runs") return go("auto-session", { taskId: context.taskId!, runId: String(item.id) }, true);
      if (["files", "team-files", "changes"].includes(operationId)) return go(operationId === "team-files" ? "team-file" : operationId === "changes" ? "diff" : "file", { path: String(item.path) }, true);
    }
    setTrail([...trail, picked[0]]); setSelected(0); setOffset(0);
  });
  const start = Math.max(0, Math.min(selected - Math.floor(height / 2), entries.length - height));
  return <Box flexDirection="column">
    {data && typeof data === "object" && !Array.isArray(data) && "path" in data ? <Text color="cyan">{String((data as Record<string, unknown>).path)}{(data as Record<string, unknown>).binary ? " · 二进制文件" : ""}{(data as Record<string, unknown>).truncated ? " · 服务端已截断" : ""}</Text> : null}
    <Text dimColor>↑↓ 选择 · Enter 展开 / 打开 · ← / Esc 返回 · r 刷新 · {trail.map((key) => labels[key] ?? key).join(" / ") || "详情"}</Text>
    {current && typeof current === "object" ? <>
      {!entries.length ? <Text dimColor>暂无记录</Text> : null}
      {entries.slice(start, start + height).map(([name, value], i) => <Text key={name} inverse={start + i === selected} wrap="truncate-end">{start + i === selected ? "›" : " "} {Array.isArray(current) ? `${Number(name) + 1}.` : labels[name] ?? name}　{previewValue(value, name)}{value && typeof value === "object" ? " ›" : ""}</Text>)}
    </> : <Text>{scalar.slice(offset, offset + height).join("\n")}</Text>}
    <Text dimColor>{operationId === "conflict" ? "x 解决冲突（自动带入版本 Hash）" : operationId === "integration" ? "s 合入 · u 继续 · x 中止 · m 提交 · Enter 打开冲突" : operationId.includes("project") ? "n 新会话 · e 编辑 · g 分支 · p 置顶 · d 删除" : operationId.startsWith("team") ? "m 发消息 · c 暂停/恢复 · f 产物 · t 事件 · a 审批" : operationId.startsWith("auto") ? "e 编辑 · h 历史 · c 暂停 · u 恢复 · n 立即执行 · a 审批 · d 删除" : operationId.startsWith("market") || operationId.startsWith("preview") ? "Enter 预览 · i 安装 · n/p 下一页/上一页" : "o 打开对话 · b 分叉 · c 变更 · f 文件 · h Handoff · d 删除"}</Text>
  </Box>;
}
function wrapText(value: string, width: number): string[] {
  return sanitizeTerminalContent(value).split("\n").flatMap((line) => {
    const result: string[] = []; let row = ""; let columns = 0;
    for (const { segment } of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(line)) {
      const size = stringWidth(segment); if (row && columns + size > width) { result.push(row); row = ""; columns = 0; } row += segment; columns += size;
    }
    return [...result, row];
  });
}
