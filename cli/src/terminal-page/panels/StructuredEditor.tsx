import React, { useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";
import { Composer } from "./Composer.js";
import { sanitizeTerminalContent } from "../../output/sanitize.js";

export const EDITOR_TEMPLATES: Record<string, unknown> = {
  weekdays: 0,
  agents: { name: "新成员", role: "worker", agentKind: "k_agent", modelId: "", reasoningEffort: "none", networkAccess: false, responsibility: "", isSupervisor: false, mcpServerIds: [], skillIds: [] },
  models: { id: "", name: "", model: "", baseUrl: "", apiKey: "", apiKeyEnv: "", enabled: true, multimodal: false, inputModalities: ["text"], supportsReasoning: false, contextWindow: 128000, maxOutputTokens: 8192, contextSafetyTokens: 1024, autoCompactEnabled: true, compactModelId: null },
  servers: { id: "", name: "", description: "", type: "stdio", command: "", args: [], env: {}, envPassthrough: [], cwd: "", url: "", bearerTokenEnv: "", headers: {}, envHeaders: {}, enabled: true },
  skills: { id: "", name: "", description: "", instructions: "", enabled: true },
  answers: { selected: [], custom: "" },
  startingState: { type: "default" },
};
const labels: Record<string, string> = { name: "名称", id: "ID", role: "角色", agentKind: "Agent 类型", modelId: "模型", responsibility: "职责", isSupervisor: "主管", networkAccess: "网络访问", permissionMode: "权限", reasoningEffort: "思考强度", mcpServerIds: "本轮 MCP", skillIds: "本轮 Skill", enabled: "启用", baseUrl: "服务地址", apiKey: "密钥", apiKeyEnv: "密钥环境变量", inputModalities: "输入模态", instructions: "指令正文", description: "说明", type: "类型", env: "环境变量", headers: "请求头", content: "内容", path: "路径", selected: "选择答案", custom: "补充答案" };

type Trail = Array<string | number>;
function at(root: unknown, trail: Trail): unknown { return trail.reduce<unknown>((value, key) => (value as Record<string | number, unknown>)?.[key], root); }
export function updateStructuredValue(root: unknown, trail: Trail, value: unknown): unknown {
  if (!trail.length) return value;
  const copy = structuredClone(root) as Record<string | number, unknown>;
  const parent = at(copy, trail.slice(0, -1)) as Record<string | number, unknown>;
  parent[trail.at(-1)!] = value;
  return copy;
}

/** 数组成员、配置对象与字典逐字段编辑；未知字段保留，不要求用户手写 JSON。 */
export function StructuredEditor({ value, fieldKey, onSave, onClose }: { value: string; fieldKey: string; onSave: (value: string) => void; onClose: () => void }): React.ReactElement {
  const { stdout } = useStdout();
  const [root, setRoot] = useState<unknown>(() => { try { return JSON.parse(value); } catch { return []; } });
  const [trail, setTrail] = useState<Trail>([]);
  const [selected, setSelected] = useState(0);
  const [editing, setEditing] = useState(false);
  const [addingKey, setAddingKey] = useState(false);
  const [draft, setDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const current = at(root, trail);
  const entries = current && typeof current === "object" ? Object.entries(current) : [];
  const picked = entries[selected];
  const height = Math.max(3, (stdout.rows ?? 24) - 10);
  function saveScalar(): void {
    if (addingKey && current && typeof current === "object" && !Array.isArray(current)) {
      if (draft.trim()) setRoot(updateStructuredValue(root, trail, { ...current, [draft.trim()]: structuredClone(fieldKey === "answers" ? EDITOR_TEMPLATES.answers : "") }));
      setAddingKey(false); setEditing(false); return;
    }
    const previous = current;
    let next: unknown = draft;
    if (typeof previous === "number") { const num = Number(draft); if (!Number.isFinite(num)) return; next = num; }
    else if (previous === null && draft === "null") next = null;
    setRoot(updateStructuredValue(root, trail, next)); setEditing(false); setTrail(trail.slice(0, -1));
  }
  useInput((input, key) => {
    if (key.ctrl && input === "c") return onClose();
    if (key.ctrl && input === "s") return onSave(JSON.stringify(root));
    if (key.escape) {
      if (editing) { setEditing(false); setAddingKey(false); if (!addingKey) setTrail(trail.slice(0, -1)); return; }
      if (confirmDelete) return setConfirmDelete(false);
      if (trail.length) { setTrail(trail.slice(0, -1)); setSelected(0); } else onClose();
      return;
    }
    if (editing) { if (key.return && !key.meta) saveScalar(); return; }
    if (confirmDelete) {
      if (key.return && picked) {
        const next = structuredClone(current) as Record<string, unknown>;
        if (Array.isArray(next)) next.splice(Number(picked[0]), 1); else delete next[picked[0]];
        setRoot(updateStructuredValue(root, trail, next)); setSelected(Math.max(0, selected - 1)); setConfirmDelete(false);
      }
      return;
    }
    if (key.upArrow) return setSelected((n) => (n + entries.length - 1) % Math.max(1, entries.length));
    if (key.downArrow) return setSelected((n) => (n + 1) % Math.max(1, entries.length));
    if (input === "a" && Array.isArray(current)) {
      const template = trail.length === 0 ? EDITOR_TEMPLATES[fieldKey] : undefined;
      setRoot(updateStructuredValue(root, trail, [...current, structuredClone(template ?? "")])); setSelected(current.length); return;
    }
    if (input === "a" && current && typeof current === "object") { setAddingKey(true); setEditing(true); setDraft(""); return; }
    if (input === "d" && picked) return setConfirmDelete(true);
    if (key.return && picked) {
      const nextTrail = [...trail, Array.isArray(current) ? Number(picked[0]) : picked[0]];
      if (typeof picked[1] === "boolean") { setRoot(updateStructuredValue(root, nextTrail, !picked[1])); return; }
      setTrail(nextTrail); setSelected(0);
      if (!picked[1] || typeof picked[1] !== "object") { setDraft(picked[1] === null ? "null" : String(picked[1])); setEditing(true); }
    }
  });
  const start = Math.max(0, Math.min(selected - Math.floor(height / 2), entries.length - height));
  return <Box flexDirection="column" paddingX={1}>
    <Text bold color="cyan">结构化编辑 · {fieldKey}{trail.length ? ` / ${trail.join(" / ")}` : ""}</Text>
    <Text dimColor>↑↓ 选择 · Enter 编辑 / 切换布尔 · a 新增 · d 删除 · Ctrl+S 保存 · Esc 返回</Text>
    {editing ? <>
      <Text>{addingKey ? "新增字段名称" : labels[String(trail.at(-1))] ?? String(trail.at(-1))} · Enter 保存 · Alt+Enter 换行</Text>
      <Composer mask={/apiKey|token|secret|password|env|headers/i.test(trail.join(" "))} value={draft} disabled={false} focused captureDigits={false} suppressKeys onChange={setDraft} onSubmit={() => {}} />
    </> : <>
      {!entries.length ? <Text dimColor>暂无条目，按 a 新增。</Text> : null}
      {entries.slice(start, start + height).map(([name, item], i) => <Text key={name} inverse={start + i === selected} wrap="truncate-end">{start + i === selected ? "›" : " "} {Array.isArray(current) ? `${Number(name) + 1}.` : labels[name] ?? name}　{previewValue(item, name)}</Text>)}
    </>}
    {confirmDelete ? <Text color="yellow">删除此条目？Enter 确认 · Esc 返回</Text> : null}
  </Box>;
}
export function previewValue(value: unknown, key = ""): string {
  if (/apiKey|token|secret|password/i.test(key)) return value ? "••••••" : "未设置";
  if (Array.isArray(value)) return `${value.length} 项`;
  if (value && typeof value === "object") { const v = value as Record<string, unknown>; return sanitizeTerminalContent([v.name ?? v.title ?? (typeof v.content === "string" ? v.content.slice(0, 80) : undefined) ?? v.id ?? `${Object.keys(v).length} 个字段`, v.status, typeof v.completedTaskCount === "number" ? `${v.completedTaskCount}/${v.taskCount} 任务` : "", v.nextRunAt, v.errorMessage ?? v.error].filter(Boolean).join(" · ")); }
  return sanitizeTerminalContent(value === true ? "✓ 是" : value === false ? "○ 否" : value === null ? "null" : String(value)).replace(/\n/g, " ↵ ");
}
