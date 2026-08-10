import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  createScheduledTask, deleteScheduledTask, getScheduledRunSession, listScheduledRuns, listScheduledTasks,
  pauseScheduledTask, resumeScheduledTask, runScheduledTaskNow, updateScheduledTask
} from "../scheduled-tasks/api";
import type { ScheduledRun, ScheduledTask, ScheduledTaskInput } from "../scheduled-tasks/types";
import type { DetectedAgent, ModelProfile, RuntimeOption, SessionState } from "../types";
import { StaticConversationTranscript } from "./ConversationTranscript";

type Props = {
  models: ModelProfile[];
  agents: DetectedAgent[];
  mcpServers: RuntimeOption[];
  skills: RuntimeOption[];
};

const weekdays = [
  [1, "周一"], [2, "周二"], [3, "周三"], [4, "周四"], [5, "周五"], [6, "周六"], [7, "周日"]
] as const;

function initialInput(models: ModelProfile[], agents: DetectedAgent[]): ScheduledTaskInput {
  const agentKind = agents[0]?.kind ?? "k_agent";
  const modelId = agentKind === "k_agent" ? models.find((item) => item.enabled)?.id ?? "" : agents[0]?.models?.[0]?.id ?? "";
  const now = new Date(Date.now() + 5 * 60_000);
  const localDate = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  return {
    name: "", prompt: "", scheduleKind: "once",
    localDate,
    weekdays: [], localTime: now.toTimeString().slice(0, 5),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
    agentKind, modelId, reasoningEffort: "none", mcpServerIds: [], skillIds: []
  };
}

export function ScheduledTasksView({ models, agents, mcpServers, skills }: Props) {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [runs, setRuns] = useState<ScheduledRun[]>([]);
  const [openedRunId, setOpenedRunId] = useState<string | null>(null);
  const [openedRunSession, setOpenedRunSession] = useState<SessionState | null>(null);
  const [editing, setEditing] = useState<ScheduledTask | "new" | null>(null);
  const [draft, setDraft] = useState(() => initialInput(models, agents));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const selected = tasks.find((task) => task.id === selectedId) ?? null;

  async function refresh() {
    try {
      const next = await listScheduledTasks();
      setTasks(next);
      setSelectedId((current) => current && next.some((task) => task.id === current) ? current : next[0]?.id ?? null);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "定时任务加载失败");
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 3000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setOpenedRunId(null);
    setOpenedRunSession(null);
    if (!selectedId) { setRuns([]); return; }
    void listScheduledRuns(selectedId).then(setRuns).catch(() => setRuns([]));
  }, [selectedId, tasks.find((task) => task.id === selectedId)?.latestRun?.id]);

  async function toggleRunResult(run: ScheduledRun) {
    if (!selectedId || !run.sessionId) return;
    if (openedRunId === run.id) {
      setOpenedRunId(null); setOpenedRunSession(null); return;
    }
    setBusy(true); setError("");
    try {
      setOpenedRunSession(await getScheduledRunSession(selectedId, run.id));
      setOpenedRunId(run.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "执行结果加载失败");
    } finally { setBusy(false); }
  }

  const availableModels = useMemo(() => {
    if (draft.agentKind === "k_agent") return models.filter((item) => item.enabled);
    return agents.find((item) => item.kind === draft.agentKind)?.models ?? [];
  }, [agents, draft.agentKind, models]);

  function beginCreate() {
    setDraft(initialInput(models, agents));
    setEditing("new");
    setError("");
  }

  function beginEdit(task: ScheduledTask) {
    const { id: _id, status: _status, nextRunAt: _next, createdAt: _created, updatedAt: _updated, latestRun: _latest, ...input } = task;
    setDraft(input);
    setEditing(task);
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const payload = {
        ...draft,
        localDate: draft.scheduleKind === "once" ? draft.localDate : null,
        weekdays: draft.scheduleKind === "weekly" ? draft.weekdays : []
      };
      const saved = editing === "new"
        ? await createScheduledTask(payload)
        : await updateScheduledTask((editing as ScheduledTask).id, payload);
      setEditing(null);
      await refresh();
      setSelectedId(saved.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally { setBusy(false); }
  }

  async function mutate(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  }

  return (
    <section className="scheduled-page">
      <header className="scheduled-topbar">
        <div><small>自动执行</small><h1>定时任务</h1><p>在指定时间自动开始一轮新会话</p></div>
        <button className="scheduled-primary" type="button" onClick={beginCreate}>＋ 创建任务</button>
      </header>
      {error && <div className="scheduled-error" role="alert"><span><strong>暂时无法读取定时任务</strong><small>{error}</small></span><button type="button" onClick={() => void refresh()}>重新加载</button></div>}
      <div className={`scheduled-layout ${selected ? "has-selection" : ""}`}>
        <section className="scheduled-list" aria-label="定时任务列表">
          <div className="scheduled-list-heading"><span>任务</span><b>{tasks.filter((task) => task.status === "active").length} 启用</b></div>
          {!tasks.length && <div className="scheduled-empty"><strong>还没有定时任务</strong><p>创建后，即使关闭页面，Access Layer 仍会按计划执行。</p><button type="button" onClick={beginCreate}>创建第一个任务</button></div>}
          {tasks.map((task) => (
            <button key={task.id} type="button" className={`scheduled-task-row ${selectedId === task.id ? "active" : ""}`} onClick={() => setSelectedId(task.id)}>
              <span className={`scheduled-dot ${task.status}`} />
              <span><strong>{task.name}</strong><small>{scheduleLabel(task)} · {task.nextRunAt ? `下次 ${formatDate(task.nextRunAt)}` : "无后续计划"}</small></span>
              <em className={task.latestRun?.status ?? "idle"}>{runStatus(task.latestRun?.status)}</em>
            </button>
          ))}
        </section>
        {selected && <section className="scheduled-detail">
          <>
            <header><div><small>{selected.status === "active" ? "已启用" : "已暂停"}</small><h2>{selected.name}</h2></div><div className="scheduled-actions">
              <button type="button" disabled={busy} onClick={() => void mutate(() => runScheduledTaskNow(selected.id))}>立即运行</button>
              <button type="button" disabled={busy} onClick={() => void mutate(() => selected.status === "active" ? pauseScheduledTask(selected.id) : resumeScheduledTask(selected.id))}>{selected.status === "active" ? "暂停" : "恢复"}</button>
              <button type="button" onClick={() => beginEdit(selected)}>编辑</button>
              <button className="danger" type="button" disabled={busy} onClick={() => { if (window.confirm(`删除“${selected.name}”？已生成的会话会保留。`)) void mutate(() => deleteScheduledTask(selected.id)); }}>删除</button>
            </div></header>
            <div className="scheduled-summary-grid"><article><small>计划</small><strong>{scheduleLabel(selected)}</strong></article><article><small>时区</small><strong>{selected.timezone}</strong></article><article><small>Agent / 模型</small><strong>{selected.agentKind} · {selected.modelId}</strong></article><article><small>下次执行</small><strong>{selected.nextRunAt ? formatDate(selected.nextRunAt) : "—"}</strong></article></div>
            <article className="scheduled-prompt"><small>任务提示词</small><p>{selected.prompt}</p></article>
            <h3>执行记录</h3>
            <div className="scheduled-runs">{!runs.length && <p className="empty-note">还没有执行记录</p>}{runs.map((run) => <div className="scheduled-run-entry" key={run.id}><div className="scheduled-run-row"><span className={`scheduled-dot ${run.status}`} /><span><strong>{runStatus(run.status)}</strong><small>{formatDate(run.scheduledFor)} · {run.triggerType === "manual" ? "手动" : "计划"}{run.errorMessage ? ` · ${run.errorMessage}` : ""}</small></span>{run.sessionId && <button type="button" disabled={busy} onClick={() => void toggleRunResult(run)}>{openedRunId === run.id ? "收起结果" : "查看结果"}</button>}</div>{openedRunId === run.id && openedRunSession && <section className="scheduled-run-result" aria-label="定时任务执行结果"><StaticConversationTranscript session={openedRunSession} /></section>}</div>)}</div>
          </>
        </section>}
      </div>
      {editing && <div className="scheduled-drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setEditing(null); }}><form className="scheduled-drawer" onSubmit={submit}>
        <header><div><small>{editing === "new" ? "NEW AUTOMATION" : "EDIT AUTOMATION"}</small><h2>{editing === "new" ? "创建定时任务" : "编辑定时任务"}</h2></div><button type="button" onClick={() => setEditing(null)}>×</button></header>
        <label>任务名称<input required maxLength={120} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="例如：每日整理项目进展" /></label>
        <label>提示词<textarea required rows={6} maxLength={20000} value={draft.prompt} onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} placeholder="描述到点后需要 Agent 完成的工作" /></label>
        <div className="scheduled-form-row"><label>频率<FrequencyPicker value={draft.scheduleKind} onChange={(scheduleKind) => setDraft({ ...draft, scheduleKind })} /></label><label>时间<TimePicker value={draft.localTime} onChange={(localTime) => setDraft({ ...draft, localTime })} /></label></div>
        {draft.scheduleKind === "once" && <label>日期<input required type="date" value={draft.localDate ?? ""} onChange={(e) => setDraft({ ...draft, localDate: e.target.value })} /></label>}
        {draft.scheduleKind === "weekly" && <fieldset><legend>执行日</legend><div className="scheduled-weekdays">{weekdays.map(([day, label]) => <button className={draft.weekdays.includes(day) ? "selected" : ""} type="button" key={day} onClick={() => setDraft({ ...draft, weekdays: draft.weekdays.includes(day) ? draft.weekdays.filter((item) => item !== day) : [...draft.weekdays, day] })}>{label}</button>)}</div></fieldset>}
        <label>时区<input required value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} /></label>
        <div className="scheduled-form-row"><label>Agent<select value={draft.agentKind} onChange={(e) => { const kind = e.target.value; const agent = agents.find((item) => item.kind === kind); setDraft({ ...draft, agentKind: kind, modelId: kind === "k_agent" ? models.find((item) => item.enabled)?.id ?? "" : agent?.models?.[0]?.id ?? "" }); }}>{agents.map((agent) => <option key={agent.kind} value={agent.kind}>{agent.name}</option>)}</select></label><label>模型<select required value={draft.modelId} onChange={(e) => setDraft({ ...draft, modelId: e.target.value })}>{availableModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label></div>
        <label>推理强度<select value={draft.reasoningEffort} onChange={(e) => setDraft({ ...draft, reasoningEffort: e.target.value as ScheduledTaskInput["reasoningEffort"] })}><option value="none">默认</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="max">最高</option></select></label>
        <CapabilityField title="MCP" items={mcpServers} selected={draft.mcpServerIds} onChange={(mcpServerIds) => setDraft({ ...draft, mcpServerIds })} />
        <CapabilityField title="Skills" items={skills} selected={draft.skillIds} onChange={(skillIds) => setDraft({ ...draft, skillIds })} />
        <footer><button type="button" onClick={() => setEditing(null)}>取消</button><button className="scheduled-primary" disabled={busy} type="submit">{busy ? "保存中…" : "保存任务"}</button></footer>
      </form></div>}
    </section>
  );
}

function CapabilityField({ title, items, selected, onChange }: { title: string; items: RuntimeOption[]; selected: string[]; onChange: (ids: string[]) => void }) {
  return <fieldset><legend>{title}</legend><div className="scheduled-capabilities">{items.filter((item) => item.enabled).map((item) => <label key={item.id}><input type="checkbox" checked={selected.includes(item.id)} onChange={() => onChange(selected.includes(item.id) ? selected.filter((id) => id !== item.id) : [...selected, item.id])} />{item.name}</label>)}</div></fieldset>;
}

function scheduleLabel(task: Pick<ScheduledTask, "scheduleKind" | "localDate" | "weekdays" | "localTime">) {
  if (task.scheduleKind === "once") return `${task.localDate} ${task.localTime}`;
  if (task.scheduleKind === "daily") return `每天 ${task.localTime}`;
  return `${task.weekdays.map((day) => weekdays.find(([value]) => value === day)?.[1]).join("、")} ${task.localTime}`;
}

function runStatus(status?: ScheduledRun["status"] | "idle") {
  return ({ queued: "等待执行", running: "执行中", succeeded: "成功", failed: "失败", missed: "已错过", idle: "未执行" } as const)[status ?? "idle"];
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

function FrequencyPicker({ value, onChange }: { value: ScheduledTaskInput["scheduleKind"]; onChange: (value: ScheduledTaskInput["scheduleKind"]) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const options = [{ value: "once", label: "仅一次" }, { value: "daily", label: "每天" }, { value: "weekly", label: "每周" }] as const;
  const selected = options.find((option) => option.value === value) ?? options[0];

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return <div className={`scheduled-frequency-picker ${open ? "open" : ""}`} ref={rootRef}>
    <button className="scheduled-frequency-trigger" type="button" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
      <span>{selected.label}</span><i aria-hidden="true">⌄</i>
    </button>
    {open && <div className="scheduled-frequency-popover" role="listbox" aria-label="执行频率">{options.map((option) => <button className={option.value === value ? "selected" : ""} type="button" role="option" aria-selected={option.value === value} key={option.value} onClick={() => { onChange(option.value); setOpen(false); }}><span>{option.label}</span>{option.value === value && <i aria-hidden="true">✓</i>}</button>)}</div>}
  </div>;
}

function TimePicker({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const [hour = "00", minute = "00"] = value.split(":");
  const hours = Array.from({ length: 24 }, (_, index) => String(index).padStart(2, "0"));
  const minutes = Array.from({ length: 60 }, (_, index) => String(index).padStart(2, "0"));

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    // Center values by moving only their list containers. `scrollIntoView` would
    // also move the drawer/page even when the popover already fits on screen.
    requestAnimationFrame(() => rootRef.current?.querySelectorAll<HTMLElement>(".scheduled-time-column button.selected").forEach((item) => {
      const list = item.parentElement;
      if (list) list.scrollTop = item.offsetTop - (list.clientHeight - item.offsetHeight) / 2;
    }));
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return <div className={`scheduled-time-picker ${open ? "open" : ""}`} ref={rootRef}>
    <button className="scheduled-time-trigger" type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
      <span>{hour}:{minute}</span><i aria-hidden="true">◷</i>
    </button>
    {open && <div className="scheduled-time-popover" role="dialog" aria-label="选择执行时间">
      <div className="scheduled-time-column"><strong>时</strong><div role="listbox" aria-label="小时">{hours.map((item) => <button className={item === hour ? "selected" : ""} type="button" role="option" aria-selected={item === hour} key={item} onClick={() => onChange(`${item}:${minute}`)}>{item}</button>)}</div></div>
      <div className="scheduled-time-column"><strong>分</strong><div role="listbox" aria-label="分钟">{minutes.map((item) => <button className={item === minute ? "selected" : ""} type="button" role="option" aria-selected={item === minute} key={item} onClick={() => onChange(`${hour}:${item}`)}>{item}</button>)}</div></div>
    </div>}
  </div>;
}
