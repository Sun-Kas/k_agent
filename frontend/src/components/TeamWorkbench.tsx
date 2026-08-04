import { useEffect, useMemo, useRef, useState } from "react";

import {
  getAgentsCatalog,
  getModelsConfig,
  getRuntimeCatalog,
  resolveApproval
} from "../api/agui";
import { MarkdownContent } from "./MarkdownContent";
import {
  commandTeam,
  createTeam,
  getTeam,
  getTeamEvents,
  listTeams,
  sendTeamMessage,
  subscribeTeamEvents
} from "../team/api";
import type {
  AgentKind,
  DetectedAgent,
  ModelProfile,
  RuntimeOption
} from "../types";
import type {
  TeamAgentDraft,
  TeamArtifact,
  TeamEvent,
  TeamSnapshot,
  TeamSummary,
  TeamTask
} from "../team/types";


const BUILTIN_AGENTS: DetectedAgent[] = [
  { kind: "k_agent", name: "K Agent", available: true },
  { kind: "codex", name: "Codex", available: false },
  { kind: "claude_code", name: "Claude Code", available: false }
];

const STATUS_LABEL: Record<string, string> = {
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
  failed: "需要处理",
  cancelled: "已取消",
  pending: "等待",
  ready: "可认领",
  claimed: "已认领",
  idle: "空闲",
  busy: "工作中",
  waiting: "等待审批",
  spawning: "正在启动",
  stopped: "已停止"
};


export function TeamWorkbench({ onBack }: { onBack: () => void }) {
  const [teams, setTeams] = useState<TeamSummary[]>([]);
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null);
  const [team, setTeam] = useState<TeamSnapshot | null>(null);
  const [events, setEvents] = useState<TeamEvent[]>([]);
  const [agents, setAgents] = useState<DetectedAgent[]>(BUILTIN_AGENTS);
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [mcpServers, setMcpServers] = useState<RuntimeOption[]>([]);
  const [skills, setSkills] = useState<RuntimeOption[]>([]);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [selectedArtifact, setSelectedArtifact] = useState<TeamArtifact | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const lastSeqRef = useRef(0);
  const refreshTimerRef = useRef<number | null>(null);

  useEffect(() => {
    void refreshCatalogs();
    void refreshTeams();
    return () => {
      if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!selectedTeamId) return;
    let cancelled = false;
    setTeam(null);
    setEvents([]);
    setSelectedArtifact(null);
    setSelectedTaskId(null);
    lastSeqRef.current = 0;
    void getTeam(selectedTeamId).then(async (snapshot) => {
      // Load the recent durable tail so an open run drawer survives refresh;
      // using seq rather than an in-memory buffer also covers Access restarts.
      const history = await getTeamEvents(selectedTeamId, Math.max(0, snapshot.lastEventSeq - 500));
      if (cancelled) return;
      setTeam(snapshot);
      setEvents(history.slice(-500));
      lastSeqRef.current = Math.max(snapshot.lastEventSeq, history.at(-1)?.seq ?? 0);
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "团队加载失败");
    });
    return () => { cancelled = true; };
  }, [selectedTeamId]);

  useEffect(() => {
    if (!selectedTeamId || !team) return;
    return subscribeTeamEvents(
      selectedTeamId,
      lastSeqRef.current,
      (event) => {
        if (event.seq <= lastSeqRef.current) return;
        lastSeqRef.current = event.seq;
        setEvents((current) => [...current.filter((item) => item.eventId !== event.eventId), event].slice(-500));
        scheduleTeamRefresh(selectedTeamId);
      },
      setConnected
    );
  }, [selectedTeamId, team?.id]);

  async function refreshCatalogs() {
    try {
      const [agentCatalog, modelCatalog, runtimeCatalog] = await Promise.all([
        getAgentsCatalog(), getModelsConfig(), getRuntimeCatalog()
      ]);
      const merged = BUILTIN_AGENTS.map((fallback) =>
        agentCatalog.agents.find((agent) => agent.kind === fallback.kind) ?? fallback
      );
      setAgents(merged);
      setModels(modelCatalog.models.filter((model) => model.enabled));
      setMcpServers(runtimeCatalog.mcpServers.filter((item) => item.enabled));
      setSkills(runtimeCatalog.skills.filter((item) => item.enabled));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "运行能力目录加载失败");
    }
  }

  async function refreshTeams(preferredId?: string) {
    try {
      const next = await listTeams();
      setTeams(next);
      const target = preferredId ?? selectedTeamId ?? next[0]?.id;
      if (target) setSelectedTeamId(target);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "团队列表加载失败");
    }
  }

  function scheduleTeamRefresh(teamId: string) {
    if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = window.setTimeout(() => {
      void Promise.all([getTeam(teamId), listTeams()]).then(([snapshot, summaries]) => {
        setTeam(snapshot);
        setTeams(summaries);
      });
    }, 120);
  }

  async function issueCommand(command: "pause" | "resume" | "cancel") {
    if (!team) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await commandTeam(team.id, command);
      setTeam(updated);
      await refreshTeams(team.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "团队控制失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="team-shell">
      <header className="team-global-header">
        <button className="team-back" type="button" onClick={onBack} aria-label="返回聊天">←</button>
        <div className="team-product-mark"><span>KT</span><div><strong>Agent Team</strong><small>SUPERVISED RUNTIME</small></div></div>
        <div className="team-header-actions">
          <span className={`team-connection ${connected ? "online" : ""}`}><i />{connected ? "实时同步" : "等待事件"}</span>
          <button className="team-primary-action" type="button" onClick={() => setCreating(true)}>＋ 创建团队</button>
        </div>
      </header>

      <div className="team-layout">
        <aside className="team-nav">
          <div className="team-nav-heading"><span>团队</span><b>{teams.length}</b></div>
          <div className="team-list">
            {teams.map((item) => {
              const progress = item.taskCount ? Math.round(item.completedTaskCount / item.taskCount * 100) : 0;
              return (
                <button
                  className={`team-list-item ${selectedTeamId === item.id && !creating ? "active" : ""}`}
                  key={item.id}
                  type="button"
                  onClick={() => { setCreating(false); setSelectedTeamId(item.id); }}
                >
                  <span className={`team-list-status ${item.status}`} />
                  <span><strong>{item.name}</strong><small>{item.agentCount} Agents · {progress}%</small></span>
                  <i>›</i>
                </button>
              );
            })}
            {!teams.length && <p className="team-empty-copy">还没有团队。创建一个团队，让多个 Agent 以明确边界协同工作。</p>}
          </div>
          <div className="team-nav-note">
            <span>LOCAL FIRST</span>
            <p>任务、Mailbox 和 Artifact 保存在本地 Team Runtime。</p>
          </div>
        </aside>

        <main className="team-main">
          {error && <div className="team-error" role="alert"><span>!</span><p>{error}</p><button type="button" onClick={() => setError(null)}>×</button></div>}
          {creating
            ? (
              <TeamComposer
                agents={agents}
                models={models}
                mcpServers={mcpServers}
                skills={skills}
                onCancel={() => setCreating(false)}
                onCreated={async (snapshot) => {
                  setCreating(false);
                  setTeam(snapshot);
                  setSelectedTeamId(snapshot.id);
                  await refreshTeams(snapshot.id);
                }}
              />
            )
            : team
              ? (
                <TeamDashboard
                  team={team}
                  events={events}
                  busy={busy}
                  selectedArtifact={selectedArtifact}
                  selectedTaskId={selectedTaskId}
                  onArtifactSelect={setSelectedArtifact}
                  onTaskSelect={setSelectedTaskId}
                  onCommand={issueCommand}
                  onMessage={async (recipientId, content) => {
                    const updated = await sendTeamMessage(team.id, recipientId, content);
                    setTeam(updated);
                  }}
                />
              )
              : <TeamWelcome onCreate={() => setCreating(true)} />}
        </main>
      </div>
    </div>
  );
}


function TeamWelcome({ onCreate }: { onCreate: () => void }) {
  return (
    <section className="team-welcome">
      <span className="team-welcome-orbit"><i /><b>3</b></span>
      <p className="team-kicker">AGENT TEAM RUNTIME</p>
      <h1>把复杂目标，交给一支<br /><em>可监督的 Agent 团队。</em></h1>
      <p>独立模型、MCP、Skill 与工作空间。协作过程随时可见、可暂停、可恢复。</p>
      <button className="team-primary-action large" type="button" onClick={onCreate}>创建第一个团队 <span>→</span></button>
    </section>
  );
}


function TeamComposer({
  agents,
  models,
  mcpServers,
  skills,
  onCancel,
  onCreated
}: {
  agents: DetectedAgent[];
  models: ModelProfile[];
  mcpServers: RuntimeOption[];
  skills: RuntimeOption[];
  onCancel: () => void;
  onCreated: (team: TeamSnapshot) => void;
}) {
  const [name, setName] = useState("产品交付团队");
  const [goal, setGoal] = useState("");
  const [maxParallel, setMaxParallel] = useState(4);
  const [mode, setMode] = useState<"auto" | "manual">("manual");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [members, setMembers] = useState<TeamAgentDraft[]>(() => defaultMembers(agents));

  function updateMember(index: number, changes: Partial<TeamAgentDraft>) {
    setMembers((current) => current.map((member, itemIndex) =>
      itemIndex === index ? { ...member, ...changes } : member
    ));
  }

  async function submit() {
    if (!goal.trim()) {
      setError("请先描述团队目标");
      return;
    }
    if (!members.length) {
      setError("团队至少需要一个 Agent");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const snapshot = await createTeam({
        name: name.trim() || "Agent Team",
        goal: goal.trim(),
        mode,
        maxParallel: Math.min(maxParallel, members.length),
        agents: members.map((member, index) => ({
          ...member,
          isSupervisor: index === 0
        }))
      });
      onCreated(snapshot);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "团队创建失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="team-composer-page">
      <header className="team-page-title">
        <div><p className="team-kicker">NEW TEAM</p><h1>组建 Agent 团队</h1><span>每个成员都拥有独立运行时和能力边界。</span></div>
        <button className="team-quiet-button" type="button" onClick={onCancel}>取消</button>
      </header>

      <div className="team-composer-grid">
        <div className="team-form-panel">
          <label className="team-field"><span>团队名称</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <div className="team-mode-switch" role="group" aria-label="组队方式">
            <button className={mode === "manual" ? "active" : ""} type="button" onClick={() => setMode("manual")}><strong>手动组队</strong><span>明确指定每个 Agent</span></button>
            <button className={mode === "auto" ? "active" : ""} type="button" onClick={() => setMode("auto")}><strong>自动调度</strong><span>按职责自主认领任务</span></button>
          </div>
          <label className="team-field"><span>团队目标</span><textarea rows={6} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="描述最终需要交付的结果、约束和质量要求…" /></label>
          <label className="team-field compact"><span>最大并行任务</span><input type="number" min={1} max={12} value={maxParallel} onChange={(event) => setMaxParallel(Number(event.target.value) || 1)} /></label>
          <div className="team-runtime-assurance"><span>✓</span><p><strong>运行时保证</strong>普通工具失败只返回对应 Agent；暂停和刷新不会丢失任务状态。</p></div>
        </div>

        <div className="team-member-panel">
          <div className="team-member-heading"><div><strong>团队成员</strong><span>{members.length} 个 Agent</span></div><button type="button" onClick={() => setMembers((current) => [...current, newMember(agents, current.length)])}>＋ 添加成员</button></div>
          <div className="team-member-list">
            {members.map((member, index) => (
              <MemberEditor
                key={`${index}-${member.agentKind}`}
                index={index}
                member={member}
                detectedAgents={agents}
                models={models}
                mcpServers={mcpServers}
                skills={skills}
                onChange={(changes) => updateMember(index, changes)}
                onRemove={() => setMembers((current) => current.filter((_, itemIndex) => itemIndex !== index))}
              />
            ))}
          </div>
        </div>
      </div>
      {error && <p className="team-form-error">{error}</p>}
      <footer className="team-composer-footer"><span>创建后 Scheduler 会按职责生成 Worker Task，并由主管综合 Artifact。</span><button className="team-primary-action large" type="button" disabled={submitting} onClick={() => void submit()}>{submitting ? "正在创建…" : "创建并开始运行"} <b>→</b></button></footer>
    </section>
  );
}


function MemberEditor({
  index, member, detectedAgents, models, mcpServers, skills, onChange, onRemove
}: {
  index: number;
  member: TeamAgentDraft;
  detectedAgents: DetectedAgent[];
  models: ModelProfile[];
  mcpServers: RuntimeOption[];
  skills: RuntimeOption[];
  onChange: (changes: Partial<TeamAgentDraft>) => void;
  onRemove: () => void;
}) {
  const runtime = detectedAgents.find((agent) => agent.kind === member.agentKind);
  const availableModels = member.agentKind === "k_agent"
    ? models.map((model) => ({ id: model.id, name: model.name }))
    : runtime?.models ?? [];
  return (
    <article className={`team-member-card ${index === 0 ? "supervisor" : ""}`}>
      <header><span className={`team-agent-avatar kind-${member.agentKind}`}>{agentMonogram(member.agentKind)}</span><div><b>{index === 0 ? "主管 Agent" : `成员 ${String(index + 1).padStart(2, "0")}`}</b><small className={runtime?.available ? "available" : "unavailable"}>{runtime?.available ? "运行时可用" : "运行时未检测到"}</small></div>{index > 0 && <button type="button" onClick={onRemove} aria-label="移除成员">×</button>}</header>
      <div className="team-member-fields">
        <label><span>运行时</span><select value={member.agentKind} onChange={(event) => onChange({ agentKind: event.target.value as AgentKind, modelId: "" })}>{detectedAgents.map((agent) => <option key={agent.kind} value={agent.kind}>{agent.name}{agent.available ? "" : " · 未检测"}</option>)}</select></label>
        <label><span>角色</span><input value={member.role} onChange={(event) => onChange({ role: event.target.value })} /></label>
        <label><span>名称</span><input value={member.name} onChange={(event) => onChange({ name: event.target.value })} /></label>
        <label><span>模型</span><select value={member.modelId ?? ""} onChange={(event) => onChange({ modelId: event.target.value })}><option value="">运行时默认</option>{availableModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>
      </div>
      <label className="team-member-responsibility"><span>职责与期望成果</span><textarea rows={2} value={member.responsibility} onChange={(event) => onChange({ responsibility: event.target.value })} /></label>
      <details className="team-capability-details">
        <summary>独立能力边界 <span>{member.mcpServerIds.length} MCP · {member.skillIds.length} Skills</span></summary>
        <CapabilityPicker label="MCP" options={mcpServers} selected={member.mcpServerIds} onChange={(mcpServerIds) => onChange({ mcpServerIds })} />
        <CapabilityPicker label="Skills" options={skills} selected={member.skillIds} onChange={(skillIds) => onChange({ skillIds })} />
      </details>
    </article>
  );
}


function CapabilityPicker({ label, options, selected, onChange }: { label: string; options: RuntimeOption[]; selected: string[]; onChange: (ids: string[]) => void }) {
  return (
    <div className="team-capability-row"><span>{label}</span><div>{options.length ? options.map((option) => <label className={selected.includes(option.id) ? "selected" : ""} key={option.id}><input type="checkbox" checked={selected.includes(option.id)} onChange={() => onChange(selected.includes(option.id) ? selected.filter((id) => id !== option.id) : [...selected, option.id])} />{option.name}</label>) : <small>暂无可选项</small>}</div></div>
  );
}


function TeamDashboard({
  team, events, busy, selectedArtifact, selectedTaskId, onArtifactSelect, onTaskSelect, onCommand, onMessage
}: {
  team: TeamSnapshot;
  events: TeamEvent[];
  busy: boolean;
  selectedArtifact: TeamArtifact | null;
  selectedTaskId: string | null;
  onArtifactSelect: (artifact: TeamArtifact | null) => void;
  onTaskSelect: (taskId: string | null) => void;
  onCommand: (command: "pause" | "resume" | "cancel") => void;
  onMessage: (recipientId: string, content: string) => Promise<void>;
}) {
  const completed = team.tasks.filter((task) => task.status === "completed").length;
  const progress = team.tasks.length ? Math.round(completed / team.tasks.length * 100) : 0;
  const [message, setMessage] = useState("");
  const [recipient, setRecipient] = useState(team.supervisorAgentId);
  const selectedTask = team.tasks.find((task) => task.id === selectedTaskId) ?? null;
  const columns = useMemo(() => [
    { key: "queue", title: "待执行", tasks: team.tasks.filter((task) => ["pending", "ready", "claimed"].includes(task.status)) },
    { key: "running", title: "进行中", tasks: team.tasks.filter((task) => task.status === "running") },
    { key: "done", title: "已交付", tasks: team.tasks.filter((task) => ["completed", "failed", "cancelled"].includes(task.status)) }
  ], [team.tasks]);

  return (
    <section className="team-dashboard">
      <header className="team-dashboard-header">
        <div><p className="team-kicker">{team.mode.toUpperCase()} TEAM</p><h1>{team.name}</h1><span>{team.goal}</span></div>
        <div className="team-dashboard-controls">
          <span className={`team-state-pill ${team.status}`}><i />{STATUS_LABEL[team.status] ?? team.status}</span>
          {team.status === "running" && <button type="button" disabled={busy} onClick={() => onCommand("pause")}>Ⅱ 暂停</button>}
          {team.status === "paused" && <button type="button" disabled={busy} onClick={() => onCommand("resume")}>▶ 恢复</button>}
          {!['completed', 'cancelled'].includes(team.status) && <button className="danger" type="button" disabled={busy} onClick={() => onCommand("cancel")}>停止</button>}
        </div>
      </header>

      <div className="team-metric-strip">
        <div><span>完成进度</span><strong>{progress}%</strong><i><b style={{ width: `${progress}%` }} /></i></div>
        <div><span>在线成员</span><strong>{team.agents.filter((agent) => ["idle", "busy", "waiting"].includes(agent.status)).length}<small> / {team.agents.length}</small></strong></div>
        <div><span>Artifacts</span><strong>{team.artifacts.length}</strong></div>
        <div><span>事件序列</span><strong>#{team.lastEventSeq}</strong></div>
      </div>

      <div className="team-dashboard-grid">
        <div className="team-board-panel">
          <header className="team-section-header"><div><p className="team-kicker">SHARED TASK BOARD</p><h2>共享任务板</h2></div><span>{completed}/{team.tasks.length} 完成</span></header>
          <div className="team-board">
            {columns.map((column) => <TaskColumn key={column.key} title={column.title} tasks={column.tasks} agents={team.agents} selectedTaskId={selectedTaskId} onTaskSelect={onTaskSelect} />)}
          </div>
        </div>

        <aside className="team-roster-panel">
          <header className="team-section-header"><div><p className="team-kicker">TEAM ROSTER</p><h2>Agent 成员</h2></div></header>
          <div className="team-roster-list">{team.agents.map((agent) => {
            const agentTasks = team.tasks.filter((task) => task.ownerAgentId === agent.id);
            const latestTask = [...agentTasks].reverse().find((task) => task.status === "running") ?? agentTasks.at(-1);
            return (
            <article className={`team-roster-card ${latestTask ? "inspectable" : ""}`} key={agent.id} role={latestTask ? "button" : undefined} tabIndex={latestTask ? 0 : undefined} onClick={() => latestTask && onTaskSelect(latestTask.id)} onKeyDown={(event) => { if (latestTask && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onTaskSelect(latestTask.id); } }}>
              <span className={`team-agent-avatar kind-${agent.agentKind}`}>{agentMonogram(agent.agentKind)}</span>
              <div><strong>{agent.name}{agent.isSupervisor && <b>主管</b>}</strong><p>{agent.role} · {agent.agentKind}</p><small title={agent.creationReason}>{agent.creationReason}</small></div>
              <i className={agent.status}>{STATUS_LABEL[agent.status] ?? agent.status}{latestTask ? " ↗" : ""}</i>
            </article>
          )})}</div>
          <form className="team-mail-composer" onSubmit={(event) => { event.preventDefault(); if (!message.trim()) return; void onMessage(recipient, message.trim()).then(() => setMessage("")); }}>
            <div><span>发送到</span><select value={recipient} onChange={(event) => setRecipient(event.target.value)}>{team.agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}<option value="broadcast">全体成员</option></select></div>
            <textarea rows={2} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="补充约束或询问进度…" />
            <button type="submit" disabled={!message.trim()}>发送消息 ↗</button>
          </form>
        </aside>
      </div>

      <div className="team-lower-grid">
        <section className="team-artifact-panel">
          <header className="team-section-header"><div><p className="team-kicker">ARTIFACTS</p><h2>成果与引用</h2></div><span>{team.artifacts.length}</span></header>
          <div className="team-artifact-list">{team.artifacts.length ? team.artifacts.map((artifact) => <button className={selectedArtifact?.id === artifact.id ? "active" : ""} key={artifact.id} type="button" onClick={() => onArtifactSelect(artifact)}><span>◇</span><div><strong>{artifact.title}</strong><small>{artifact.kind} · v{artifact.version}</small></div><i>›</i></button>) : <EmptyPanel text="Agent 完成任务后会在这里发布 Artifact" />}</div>
        </section>
        <section className="team-flow-panel">
          <header className="team-section-header"><div><p className="team-kicker">TASK HANDOFF MAP</p><h2>成员任务流转</h2></div><span>发放 · 接取 · 汇聚</span></header>
          <TeamTaskFlowMap team={team} events={events} onTaskSelect={onTaskSelect} />
        </section>
      </div>

      {selectedArtifact && <div className="team-artifact-drawer" role="dialog" aria-modal="true" aria-label="Artifact 内容"><button className="team-artifact-scrim" type="button" onClick={() => onArtifactSelect(null)} aria-label="关闭" /><aside><header><div><p className="team-kicker">{selectedArtifact.uri}</p><h2>{selectedArtifact.title}</h2><span>{selectedArtifact.kind} · SHA {selectedArtifact.sha256.slice(0, 10)}</span></div><button type="button" onClick={() => onArtifactSelect(null)}>×</button></header><div className="team-artifact-content"><MarkdownContent content={selectedArtifact.content} /></div></aside></div>}
      {selectedTask && <TaskRunDrawer key={selectedTask.ownerAgentId ?? selectedTask.id} task={selectedTask} tasks={team.tasks} agents={team.agents} artifacts={team.artifacts} events={events} teamGoal={team.goal} onClose={() => onTaskSelect(null)} />}
    </section>
  );
}


function TaskColumn({ title, tasks, agents, selectedTaskId, onTaskSelect }: { title: string; tasks: TeamTask[]; agents: TeamSnapshot["agents"]; selectedTaskId: string | null; onTaskSelect: (taskId: string) => void }) {
  return (
    <section className="team-task-column"><header><strong>{title}</strong><span>{tasks.length}</span></header><div>{tasks.length ? tasks.map((task) => {
      const owner = agents.find((agent) => agent.id === task.ownerAgentId);
      return <article className={`team-task-card ${task.status} ${selectedTaskId === task.id ? "selected" : ""}`} key={task.id} role="button" tabIndex={0} onClick={() => onTaskSelect(task.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onTaskSelect(task.id); } }}><span className="team-task-type">{task.taskType === "synthesis" ? "SYNTHESIS" : "TASK"}</span><span className="team-task-inspect">查看工作记录 ↗</span><h3>{task.title}</h3><p>{task.description}</p>{task.error && <small className="team-task-error">{task.error}</small>}<footer><span>{owner ? <><b className={`kind-${owner.agentKind}`}>{agentMonogram(owner.agentKind)}</b>{owner.name}</> : "等待认领"}</span><i>{STATUS_LABEL[task.status] ?? task.status}{task.attempt ? ` · #${task.attempt}` : ""}</i></footer></article>;
    }) : <p className="team-column-empty">暂无任务</p>}</div></section>
  );
}


function TeamTaskFlowMap({ team, events, onTaskSelect }: { team: TeamSnapshot; events: TeamEvent[]; onTaskSelect: (taskId: string) => void }) {
  const supervisor = team.agents.find((agent) => agent.id === team.supervisorAgentId) ?? team.agents[0];
  const orderedTasks = [...team.tasks].sort((left, right) => {
    const leftSeq = firstTaskEventSeq(events, left.id);
    const rightSeq = firstTaskEventSeq(events, right.id);
    return leftSeq === rightSeq ? right.priority - left.priority : leftSeq - rightSeq;
  });
  return (
    <div className="team-flow-map">
      {orderedTasks.map((task) => {
        const owner = team.agents.find((agent) => agent.id === task.ownerAgentId);
        const dependencyOwners = task.dependsOn
          .map((dependencyId) => team.tasks.find((candidate) => candidate.id === dependencyId)?.ownerAgentId)
          .map((agentId) => team.agents.find((agent) => agent.id === agentId))
          .filter((agent): agent is TeamSnapshot["agents"][number] => Boolean(agent));
        const uniqueSources = dependencyOwners.filter((agent, index) =>
          dependencyOwners.findIndex((candidate) => candidate.id === agent.id) === index
        );
        const sources = uniqueSources.length ? uniqueSources : supervisor ? [supervisor] : [];
        const handoffLabel = uniqueSources.length ? "成果汇聚" : "任务发放";
        return (
          <article className={`team-flow-route ${task.status}`} key={task.id}>
            <FlowMemberNode agents={sources} fallback="团队目标" />
            <div className="team-flow-edge">
              <span>{handoffLabel}</span><i /><button type="button" onClick={() => onTaskSelect(task.id)}><small>{task.taskType === "synthesis" ? "SYNTHESIS" : "TASK"}</small><strong>{task.title}</strong><b>{STATUS_LABEL[task.status] ?? task.status}</b></button><i /><span>任务接取</span>
            </div>
            <FlowMemberNode agents={owner ? [owner] : []} fallback="等待认领" />
          </article>
        );
      })}
      {!orderedTasks.length && <EmptyPanel text="创建任务后会显示成员间的发放与接取关系" />}
    </div>
  );
}

function FlowMemberNode({ agents, fallback }: { agents: TeamSnapshot["agents"]; fallback: string }) {
  return (
    <div className={`team-flow-member ${agents.length ? "" : "empty"}`}>
      <div>{agents.slice(0, 3).map((agent) => <span className={`team-agent-avatar kind-${agent.agentKind}`} key={agent.id}>{agentMonogram(agent.agentKind)}</span>)}</div>
      <strong>{agents.length === 1 ? agents[0].name : agents.length > 1 ? `${agents[0].name} 等 ${agents.length} 人` : fallback}</strong>
      <small>{agents.length === 1 ? agents[0].role : agents.length > 1 ? "依赖任务成员" : "未分配"}</small>
    </div>
  );
}


type ConversationItem =
  | { kind: "message"; id: string; content: string; occurredAt: string }
  | { kind: "reasoning"; id: string; content: string; occurredAt: string }
  | { kind: "tool"; id: string; name: string; arguments: string; result: string; status: "running" | "waiting" | "complete" | "error"; occurredAt: string }
  | { kind: "approval"; id: string; threadId: string; runId: string; agentKind: AgentKind; category: string; title: string; message: string; detail: Record<string, unknown>; status: "pending" | "approved" | "denied" | "cancelled"; action?: string; occurredAt: string }
  | { kind: "notice"; id: string; label: string; tone: "normal" | "error" | "success"; occurredAt: string };

function TaskRunDrawer({ task, tasks, agents, artifacts, events, teamGoal, onClose }: { task: TeamTask; tasks: TeamTask[]; agents: TeamSnapshot["agents"]; artifacts: TeamArtifact[]; events: TeamEvent[]; teamGoal: string; onClose: () => void }) {
  const owner = agents.find((agent) => agent.id === task.ownerAgentId);
  const supervisor = agents.find((agent) => agent.isSupervisor) ?? agents[0];
  const ownerTasks = owner
    ? tasks.filter((candidate) => candidate.ownerAgentId === owner.id)
    : [task];
  const taskOrder = new Map(ownerTasks.map((candidate, index) => [candidate.id, index]));
  const orderedTasks = [...ownerTasks].sort((left, right) => {
    const leftSeq = firstTaskEventSeq(events, left.id);
    const rightSeq = firstTaskEventSeq(events, right.id);
    if (leftSeq !== rightSeq) return leftSeq - rightSeq;
    return (taskOrder.get(left.id) ?? 0) - (taskOrder.get(right.id) ?? 0);
  });
  const latestStart = [...events].reverse().find((event) =>
    event.type === "run.started" && orderedTasks.some((candidate) => candidate.id === event.payload.taskId)
  );
  const [expandedTaskId, setExpandedTaskId] = useState(task.id);
  const lastAutoFocusedSeqRef = useRef(latestStart?.seq ?? 0);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setExpandedTaskId(task.id);
  }, [task.id]);

  useEffect(() => {
    // A newly claimed task is the next turn in this Agent's work record. Only
    // a genuinely new run steals focus; manually opened history stays open.
    if (latestStart && latestStart.seq > lastAutoFocusedSeqRef.current) {
      lastAutoFocusedSeqRef.current = latestStart.seq;
      const nextTaskId = String(latestStart.payload.taskId ?? "");
      if (nextTaskId) setExpandedTaskId(nextTaskId);
    }
  }, [latestStart?.seq]);

  const expandedTask = orderedTasks.find((candidate) => candidate.id === expandedTaskId);
  const expandedEventCount = events.filter((event) => event.payload.taskId === expandedTaskId).length;
  useEffect(() => {
    if (expandedTask?.status === "running") {
      bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: "smooth" });
    }
  }, [expandedEventCount, expandedTask?.status]);

  return (
    <div className="team-run-drawer" role="dialog" aria-modal="true" aria-label={`${owner?.name ?? "Agent"}工作记录`}>
      <button className="team-artifact-scrim" type="button" onClick={onClose} aria-label="关闭" />
      <aside>
        <header><span className={`team-agent-avatar kind-${owner?.agentKind ?? "k_agent"}`}>{owner ? agentMonogram(owner.agentKind) : "?"}</span><div><p className="team-kicker">AGENT WORK RECORD</p><h2>{owner?.name ?? "尚未认领的任务"}</h2><span>{owner?.role ?? "-"} · {owner?.agentKind ?? "-"} · {orderedTasks.length} 个承接任务</span></div><span className={`team-state-pill ${owner?.status ?? task.status}`}><i />{STATUS_LABEL[owner?.status ?? task.status] ?? owner?.status ?? task.status}</span><button type="button" onClick={onClose}>×</button></header>
        <div className="team-run-content" ref={bodyRef}>
          {orderedTasks.map((candidate, index) => {
            const open = candidate.id === expandedTaskId;
            const taskEvents = events.filter((event) => event.payload.taskId === candidate.id);
            const items = buildConversationItems(taskEvents);
            const toolCount = items.filter((item) => item.kind === "tool").length;
            const taskArtifacts = artifacts.filter((artifact) => artifact.taskId === candidate.id);
            return (
              <section className={`team-task-session ${open ? "open" : "collapsed"} ${candidate.status}`} key={candidate.id}>
                <button className="team-task-session-summary" type="button" aria-expanded={open} onClick={() => setExpandedTaskId(open ? "" : candidate.id)}>
                  <span>{open ? "⌄" : "›"}</span><div><small>任务 {String(index + 1).padStart(2, "0")} · {candidate.taskType === "synthesis" ? "综合" : "执行"}</small><strong>{candidate.title}</strong><p>{STATUS_LABEL[candidate.status] ?? candidate.status} · {toolCount} 次工具调用 · {taskArtifacts.length} 个 Artifact</p></div><i className={candidate.status}><b />{candidate.status === "running" ? "实时输出" : STATUS_LABEL[candidate.status] ?? candidate.status}</i>
                </button>
                {open && (
                  <div className="team-task-conversation">
                    <article className="team-conversation-request">
                      <header><span>承接任务详情</span><b>{candidate.taskType === "synthesis" ? "综合任务" : "执行任务"}</b></header>
                      <div><strong>{candidate.title}</strong><p>{candidate.description}</p></div>
                      <dl><div><dt>发放者</dt><dd>{supervisor?.name ?? "团队调度器"}</dd></div><div><dt>承接者</dt><dd>{owner?.name ?? "等待认领"}</dd></div><div><dt>优先级</dt><dd>P{candidate.priority}</dd></div><div><dt>执行次数</dt><dd>{candidate.attempt}/{candidate.maxAttempts}</dd></div></dl>
                      <section><small>团队目标</small><p>{teamGoal}</p></section>
                      {candidate.dependsOn.length > 0 && <section><small>前置依赖</small><p>{candidate.dependsOn.map((dependencyId) => tasks.find((item) => item.id === dependencyId)?.title ?? dependencyId).join("、")}</p></section>}
                      {owner?.responsibility && <section><small>职责边界</small><p>{owner.responsibility}</p></section>}
                    </article>
                    {items.length ? items.map((item) => <ConversationEvent key={item.id} item={item} live={candidate.status === "running"} />) : <EmptyPanel text={candidate.status === "running" ? "Agent 正在准备响应…" : "这个任务还没有运行记录"} />}
                    {taskArtifacts.map((artifact) => <article className="team-conversation-artifact" key={artifact.id}><span>◇</span><div><small>ARTIFACT · v{artifact.version}</small><strong>{artifact.title}</strong><p>{artifact.uri}</p></div></article>)}
                    {candidate.error && <div className="team-run-error"><strong>运行错误</strong><p>{candidate.error}</p></div>}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      </aside>
    </div>
  );
}

function ConversationEvent({ item, live }: { item: ConversationItem; live: boolean }) {
  if (item.kind === "message") return <article className="team-conversation-message"><header><span>Agent</span><time>{formatTime(item.occurredAt)}</time></header><MarkdownContent content={item.content} />{live && <i className="team-stream-caret" />}</article>;
  if (item.kind === "reasoning") return <ConversationReasoning item={item} live={live} />;
  if (item.kind === "tool") return <ConversationTool item={item} />;
  if (item.kind === "approval") return <ConversationApproval item={item} />;
  return <div className={`team-conversation-notice ${item.tone}`}><i /><span>{item.label}</span><time>{formatTime(item.occurredAt)}</time></div>;
}

function ConversationReasoning({ item, live }: { item: Extract<ConversationItem, { kind: "reasoning" }>; live: boolean }) {
  const [open, setOpen] = useState(live);
  return <section className={`team-conversation-reasoning ${open ? "open" : ""}`}><button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)}><span>✦</span><strong>{live ? "思考过程" : "已完成思考"}</strong><i>⌃</i></button>{open && <p>{item.content}</p>}</section>;
}

function ConversationTool({ item }: { item: Extract<ConversationItem, { kind: "tool" }> }) {
  const [open, setOpen] = useState(item.status !== "complete");
  useEffect(() => {
    if (item.status === "error") setOpen(true);
  }, [item.status]);
  return <section className={`team-conversation-tool ${item.status} ${open ? "open" : ""}`}><button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)}><span>⌁</span><strong>调用工具</strong><code>{item.name}</code><b>{item.status === "complete" ? "已完成" : item.status === "error" ? "失败" : item.status === "waiting" ? "等待结果" : "运行中"}</b><i>⌃</i></button>{open && (item.arguments || item.result) && <div>{item.arguments && <pre>{item.arguments}</pre>}{item.result && <p>{item.result}</p>}</div>}</section>;
}

function ConversationApproval({ item }: { item: Extract<ConversationItem, { kind: "approval" }> }) {
  const [submission, setSubmission] = useState<"idle" | "submitting" | "error">("idle");
  const [optimisticAction, setOptimisticAction] = useState<string | null>(null);
  const resolvedAction = item.action ?? optimisticAction;
  const pending = item.status === "pending" && !resolvedAction;
  const preview = approvalPreview(item.detail);

  useEffect(() => {
    if (item.status !== "pending") setSubmission("idle");
  }, [item.status]);

  async function decide(action: "approve" | "deny" | "cancel", remember = false) {
    setSubmission("submitting");
    try {
      await resolveApproval(item.id, {
        threadId: item.threadId,
        runId: item.runId,
        action,
        remember,
      });
      setOptimisticAction(action);
      setSubmission("idle");
    } catch {
      setSubmission("error");
    }
  }

  const statusLabel = resolvedAction === "approve"
    ? "已允许"
    : resolvedAction === "deny"
      ? "已拒绝"
      : resolvedAction === "cancel"
        ? "已取消"
        : submission === "submitting"
          ? "正在提交"
          : "等待你的决定";
  return (
    <section className={`team-conversation-approval ${pending ? "pending" : "resolved"}`}>
      <header><span>!</span><div><small>HUMAN APPROVAL · {item.agentKind}</small><strong>{item.title}</strong></div><b>{statusLabel}</b></header>
      <p>{item.message}</p>
      {preview && <pre>{preview}</pre>}
      {submission === "error" && <p className="team-approval-error">审批提交失败或请求已失效，请刷新后重试。</p>}
      {pending && <footer><button type="button" disabled={submission === "submitting"} onClick={() => void decide("deny")}>拒绝</button><button type="button" disabled={submission === "submitting"} onClick={() => void decide("approve")}>允许一次</button><button className="primary" type="button" disabled={submission === "submitting"} onClick={() => void decide("approve", true)}>本任务始终允许</button></footer>}
    </section>
  );
}

function buildConversationItems(events: TeamEvent[]): ConversationItem[] {
  const items: ConversationItem[] = [];
  const tools = new Map<string, Extract<ConversationItem, { kind: "tool" }>>();
  const approvals = new Map<string, Extract<ConversationItem, { kind: "approval" }>>();
  for (const event of [...events].sort((left, right) => left.seq - right.seq)) {
    if (event.type === "run.output" || event.type === "run.reasoning") {
      const kind = event.type === "run.output" ? "message" : "reasoning";
      const delta = typeof event.payload.delta === "string" ? event.payload.delta : "";
      const previous = items.at(-1);
      if (previous?.kind === kind) previous.content += delta;
      else items.push({ kind, id: event.eventId, content: delta, occurredAt: event.occurredAt });
      continue;
    }
    if (event.type === "run.activity") {
      const raw = event.payload.event;
      if (!raw || typeof raw !== "object") continue;
      const value = raw as Record<string, unknown>;
      const type = String(value.type ?? "CUSTOM");
      if (type === "CUSTOM") {
        const customName = String(value.name ?? "");
        const customValue = value.value && typeof value.value === "object"
          ? value.value as Record<string, unknown>
          : {};
        if (customName === "approval_request") {
          appendApprovalRequest(items, approvals, customValue, event);
          continue;
        }
        if (customName === "approval_resolved") {
          resolveApprovalItem(approvals, customValue);
          continue;
        }
        items.push({ kind: "notice", id: event.eventId, label: "Agent 更新运行状态", tone: "normal", occurredAt: event.occurredAt });
        continue;
      }
      const toolId = String(value.toolCallId ?? event.eventId);
      let tool = tools.get(toolId);
      if (!tool) {
        tool = { kind: "tool", id: toolId, name: String(value.toolCallName ?? value.name ?? "工具"), arguments: "", result: "", status: "running", occurredAt: event.occurredAt };
        tools.set(toolId, tool);
        items.push(tool);
      }
      if (value.toolCallName || value.name) tool.name = String(value.toolCallName ?? value.name);
      if (type === "TOOL_CALL_ARGS") tool.arguments += stringifyActivityValue(value.delta ?? value.arguments);
      if (type === "TOOL_CALL_END") tool.status = "waiting";
      if (type === "TOOL_CALL_RESULT") {
        tool.result = stringifyActivityValue(value.content);
        tool.status = toolResultIsError(tool.result) ? "error" : "complete";
      }
      continue;
    }
    if (event.type === "approval.requested") {
      appendApprovalRequest(items, approvals, event.payload, event);
      continue;
    }
    if (event.type === "approval.resolved") {
      resolveApprovalItem(approvals, event.payload);
      continue;
    }
    if (event.type === "run.started") items.push({ kind: "notice", id: event.eventId, label: "Agent 开始执行任务", tone: "normal", occurredAt: event.occurredAt });
    if (event.type === "run.failed") items.push({ kind: "notice", id: event.eventId, label: "本次运行失败，调度器将按策略处理", tone: "error", occurredAt: event.occurredAt });
    if (event.type === "task.completed") items.push({ kind: "notice", id: event.eventId, label: "任务已完成并发布 Artifact", tone: "success", occurredAt: event.occurredAt });
  }
  return items;
}

function appendApprovalRequest(
  items: ConversationItem[],
  approvals: Map<string, Extract<ConversationItem, { kind: "approval" }>>,
  value: Record<string, unknown>,
  event: TeamEvent,
) {
  const id = String(value.id ?? "");
  if (!id || approvals.has(id)) return;
  const approval: Extract<ConversationItem, { kind: "approval" }> = {
    kind: "approval",
    id,
    threadId: String(value.threadId ?? ""),
    runId: String(value.runId ?? ""),
    agentKind: String(value.agentKind ?? "k_agent") as AgentKind,
    category: String(value.category ?? "tool"),
    title: String(value.title ?? "需要你的确认"),
    message: String(value.message ?? "请确认是否允许 Agent 继续。"),
    detail: value.detail && typeof value.detail === "object"
      ? value.detail as Record<string, unknown>
      : {},
    status: "pending",
    occurredAt: event.occurredAt,
  };
  approvals.set(id, approval);
  items.push(approval);
}

function resolveApprovalItem(
  approvals: Map<string, Extract<ConversationItem, { kind: "approval" }>>,
  value: Record<string, unknown>,
) {
  const approval = approvals.get(String(value.id ?? ""));
  if (!approval) return;
  const action = String(value.action ?? "cancel");
  approval.action = action;
  approval.status = action === "approve" ? "approved" : action === "deny" ? "denied" : "cancelled";
}

function approvalPreview(detail: Record<string, unknown>): string {
  const direct = detail.command ?? detail.cmd ?? detail.diff ?? detail.path;
  if (direct !== undefined) return stringifyActivityValue(direct);
  const input = detail.input;
  if (input && typeof input === "object") {
    const record = input as Record<string, unknown>;
    const nested = record.command ?? record.cmd ?? record.path;
    if (nested !== undefined) return stringifyActivityValue(nested);
  }
  const serialized = Object.keys(detail).length ? JSON.stringify(detail, null, 2) : "";
  return serialized.length > 3000 ? `${serialized.slice(0, 3000)}\n…` : serialized;
}

function firstTaskEventSeq(events: TeamEvent[], taskId: string): number {
  return events.find((event) => event.payload.taskId === taskId)?.seq ?? Number.MAX_SAFE_INTEGER;
}

function stringifyActivityValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  return JSON.stringify(value, null, 2);
}

function toolResultIsError(value: string): boolean {
  try {
    const parsed = JSON.parse(value) as { ok?: boolean; isError?: boolean };
    return parsed.ok === false || parsed.isError === true;
  } catch {
    return false;
  }
}


function EmptyPanel({ text }: { text: string }) {
  return <p className="team-panel-empty">{text}</p>;
}

function defaultMembers(agents: DetectedAgent[]): TeamAgentDraft[] {
  const available = new Map(agents.map((agent) => [agent.kind, agent.available]));
  const members: TeamAgentDraft[] = [
    { name: "团队主管", role: "Supervisor", agentKind: "k_agent", responsibility: "拆解目标、监督质量，并综合所有 Artifact。", isSupervisor: true, mcpServerIds: [], skillIds: [] }
  ];
  if (available.get("codex")) members.push({ name: "实现 Agent", role: "Engineer", agentKind: "codex", responsibility: "负责实现、验证和提交可审查的工程成果。", isSupervisor: false, mcpServerIds: [], skillIds: [] });
  if (available.get("claude_code")) members.push({ name: "审查 Agent", role: "Reviewer", agentKind: "claude_code", responsibility: "独立审查方案或实现，指出风险、冲突和遗漏。", isSupervisor: false, mcpServerIds: [], skillIds: [] });
  return members;
}

function newMember(agents: DetectedAgent[], index: number): TeamAgentDraft {
  const available = agents.find((agent) => agent.available && agent.kind !== "k_agent") ?? agents[0] ?? BUILTIN_AGENTS[0];
  return { name: `Agent ${index + 1}`, role: "Specialist", agentKind: available.kind, responsibility: "完成主管分配的专业任务并发布 Artifact。", isSupervisor: false, mcpServerIds: [], skillIds: [] };
}

function agentMonogram(kind: AgentKind): string {
  if (kind === "codex") return "CX";
  if (kind === "claude_code") return "CC";
  return "KA";
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}
