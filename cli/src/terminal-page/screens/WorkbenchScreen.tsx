import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";
import { WORKBENCH_OPERATIONS, initialOperationValues, type WorkbenchOperation } from "../../application/workbench-catalog.js";
import stringWidth from "string-width";
import { sanitizeTerminalContent } from "../../output/sanitize.js";
import { ResultExplorer, type ExplorerNavigation } from "../panels/ResultExplorer.js";
import { StructuredEditor, previewValue } from "../panels/StructuredEditor.js";
import { Composer } from "../panels/Composer.js";
import type { TerminalPageAction, TerminalPageViewModel } from "../types.js";

/** 完整功能工作台：字段编辑和结果滚动互斥，防止导航键落入普通聊天输入框。 */
export function WorkbenchScreen({ model, onAction }: { model: TerminalPageViewModel; onAction: (action: TerminalPageAction) => void }): React.ReactElement {
  const { stdout } = useStdout();
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState(0);
  const [operation, setOperation] = useState<WorkbenchOperation>();
  const [values, setValues] = useState<Record<string, string>>({});
  const [fieldIndex, setFieldIndex] = useState(0);
  const [offset, setOffset] = useState(0);
  const [mode, setMode] = useState<"menu" | "form" | "result">("menu");
  const [structured, setStructured] = useState(false);
  const [rawResult, setRawResult] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const operations = useMemo(() => WORKBENCH_OPERATIONS.filter((op) => `${op.group} ${op.title} ${op.id}`.toLowerCase().includes(filter.toLowerCase())), [filter]);
  const height = Math.max(3, (stdout.rows ?? 24) - 13);
  const result = model.workbench;
  const lines = useMemo(() => wrapWorkbenchLines(formatWorkbenchResult(result?.result), Math.max(12, (stdout.columns ?? 80) - 4)), [result?.result, stdout.columns]);
  const field = operation?.fields[fieldIndex];
  const value = field ? values[field.key] ?? "" : "";

  useEffect(() => {
    if (result?.operationId === operation?.id && result?.formValues) setValues((previous) => ({ ...previous, ...result.formValues }));
  }, [result?.formValues, operation?.id]);
  useEffect(() => { setOffset(0); }, [result?.operationId]);

  function run(): void {
    if (!operation) return;
    setConfirming(false);
    setMode("result");
    setOffset(0);
    onAction({ type: "workbench_execute", operationId: operation.id, values });
  }
  function navigate(target: ExplorerNavigation): void {
    if (result?.busy) return;
    const op = WORKBENCH_OPERATIONS.find((item) => item.id === target.operationId);
    if (!op) return;
    const next = { ...initialOperationValues(op, model.activeSessionId), ...target.values };
    setOperation(op); setValues(next); setFieldIndex(op.fields.findIndex((item) => !next[item.key]) < 0 ? 0 : op.fields.findIndex((item) => !next[item.key])); setConfirming(false); setRawResult(false);
    if (target.execute) { setMode("result"); onAction({ type: "workbench_execute", operationId: op.id, values: next }); }
    else { setMode("form"); if (op.editSource || op.local === "runtime" || op.local === "preferences") onAction({ type: "workbench_load", operationId: op.id, values: next }); }
  }
  function choose(op: WorkbenchOperation): void {
    setOperation(op);
    setValues(initialOperationValues(op, model.activeSessionId));
    setFieldIndex(0);
    setConfirming(false);
    setMode("form");
    if (op.local === "runtime" || op.local === "preferences" || (op.editSource && !op.editSource.includes(":"))) onAction({ type: "workbench_load", operationId: op.id, values: initialOperationValues(op, model.activeSessionId) });
  }
  useInput((input, key) => {
    if (structured) return;
    if (key.ctrl && input === "c") return onAction({ type: "close_workbench" });
    if (mode === "result" && !rawResult && !result?.busy) return;
    if (key.escape) {
      if (confirming) return setConfirming(false);
      if (mode === "result") return setMode("form");
      if (mode === "form") return setMode("menu");
      return onAction({ type: "close_workbench" });
    }
    if (result?.busy) return;
    if (mode === "menu") {
      if (key.upArrow) return setSelected((n) => (n + operations.length - 1) % Math.max(1, operations.length));
      if (key.downArrow) return setSelected((n) => (n + 1) % Math.max(1, operations.length));
      if (key.return && operations[selected]) return choose(operations[selected]!);
      return;
    }
    if (mode === "result") {
      if (key.upArrow) return setOffset((n) => Math.max(0, n - 1));
      if (key.downArrow) return setOffset((n) => Math.min(Math.max(0, lines.length - height), n + 1));
      if (key.pageDown) return setOffset((n) => Math.min(Math.max(0, lines.length - height), n + height));
      if (key.pageUp) return setOffset((n) => Math.max(0, n - height));
      if (input === "r") return run();
      return;
    }
    if (confirming) { if (key.return && !key.meta) run(); return; }
    if (key.ctrl && input === "l" && operation?.editSource) return onAction({ type: "workbench_load", operationId: operation.id, values });
    if ((key.ctrl && input === "s") || (key.return && !field)) {
      if (operation?.method === "GET") run(); else setConfirming(true);
      return;
    }
    if (key.return && !key.meta && field?.kind === "json") return setStructured(true);
    if (field?.choices && (key.leftArrow || key.rightArrow)) {
      const index = field.choices.indexOf(value);
      const next = field.choices[(index + field.choices.length + (key.rightArrow ? 1 : -1)) % field.choices.length]!;
      setValues((previous) => ({ ...previous, [field.key]: next })); return;
    }
    if (key.return && !key.meta && field?.kind === "boolean") { setValues((previous) => ({ ...previous, [field.key]: value === "true" ? "false" : "true" })); return; }
    if (key.return && !key.meta && field) return setFieldIndex((n) => n + 1);
    if (key.tab || key.upArrow || key.downArrow) {
      const count = (operation?.fields.length ?? 0) + 1;
      setFieldIndex((n) => (n + (key.shift || key.upArrow ? count - 1 : 1)) % count);
    }
  });

  if (structured && field) return <StructuredEditor value={value} fieldKey={field.key} onSave={(next) => { setValues((previous) => ({ ...previous, [field.key]: next })); setStructured(false); }} onClose={() => setStructured(false)} />;
  const start = Math.max(0, Math.min(selected - Math.floor(height / 2), operations.length - height));
  return <Box flexDirection="column" paddingX={1}>
    <Text bold color="cyan">全部功能 · {operation && mode !== "menu" ? `${operation.group} / ${operation.title}` : `${operations.length} 项`}</Text>
    {mode === "menu" ? <>
      <Text dimColor>↑↓ 选择 · Enter 打开 · 输入关键词筛选 · Esc 返回聊天</Text>
      {operations.slice(start, start + height).map((op, i) => <Text key={op.id} inverse={start + i === selected}>{start + i === selected ? "›" : " "} {op.group} · {op.title}  /{op.id}</Text>)}
      <Composer value={filter} disabled={false} focused captureDigits={false} suppressKeys lockInput={false} onChange={(next) => { setFilter(next); setSelected(0); }} onSubmit={() => {}} />
    </> : mode === "form" ? <>
      <Text dimColor>Tab / ↑↓ 切换字段 · Enter 下一项 · Ctrl+S 执行 · Ctrl+L 读取当前值 · Esc 返回</Text>
      {operation?.fields.length ? <Text>{fieldIndex + 1}/{operation.fields.length + 1} · {field?.label ?? "执行"}{field?.optional ? "（可选）" : ""}</Text> : null}
      {field?.choices ? <Text color="yellow">可选值：{field.choices.join(" / ")}</Text> : null}
      {field?.kind === "json" && !confirming ? <Text color="cyan">{previewValue((() => { try { return JSON.parse(value); } catch { return value; } })())} · Enter 逐项编辑 · Tab 下一字段</Text> : null}
      {field && field.kind !== "json" && !confirming ? <Composer key={`${operation?.id}:${field.key}`} value={value} disabled={false} focused captureDigits={false} suppressKeys onChange={(next) => setValues((previous) => ({ ...previous, [field.key]: next }))} onSubmit={() => {}} /> : null}
      <Text>{confirming ? "即将执行此操作。Enter 确认执行 · Esc 继续编辑" : !field ? "Enter 执行" : ""}</Text>
      <Text dimColor>{operation?.fields.map((f) => `${f.label}: ${f.kind === "secret" ? "••••" : (values[f.key] || "—").slice(0, 60)}`).slice(Math.max(0, fieldIndex - 2), Math.max(0, fieldIndex - 2) + Math.min(height, 5)).join("\n")}</Text>
      {result?.error ? <Text color="red">{result.error}</Text> : null}
    </> : <>
      <Text dimColor>{result?.busy ? "请求中…" : `↑↓ / PgUp/PgDn 滚动 · r 重新执行 · Esc 编辑 · ${offset + 1}-${Math.min(lines.length, offset + height)} / ${lines.length}`}</Text>
      {result?.error ? <Text color="red">{result.error}</Text> : null}
      {result?.busy ? <Text>正在读取状态…</Text> : !rawResult && operation ? <ResultExplorer key={operation.id} operationId={operation.id} data={result?.result} context={values} onNavigate={navigate} onRefresh={() => { if (operation.method === "GET") run(); else setMode("form"); }} onBack={() => setMode("form")} /> : <Text wrap="wrap">{lines.slice(offset, offset + height).join("\n")}</Text>}
    </>}
  </Box>;
}

/** 结构化状态完整可读，控制字符先过滤；不得把服务端内容当成终端控制序列执行。 */
export function formatWorkbenchResult(value: unknown): string {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    if (typeof record.diff === "string" || typeof record.content === "string") {
      const { content, diff, ...meta } = record;
      return sanitizeTerminalContent(`${JSON.stringify(meta, null, 2)}\n\n${diff ?? content}`);
    }
  }
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "选择功能后执行，结果在这里显示", null, 2);
  return sanitizeTerminalContent(text);
}

/** 先按终端列宽折行再分页，长路径、中文和 Diff 行不能把页面挤出视口。 */
export function wrapWorkbenchLines(text: string, width: number): string[] {
  return text.split("\n").flatMap((line) => {
    const result: string[] = [];
    let current = "";
    let columns = 0;
    for (const { segment } of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(line)) {
      const size = stringWidth(segment);
      if (current && columns + size > width) { result.push(current); current = ""; columns = 0; }
      current += segment;
      columns += size;
    }
    result.push(current);
    return result;
  });
}
