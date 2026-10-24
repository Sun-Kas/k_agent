import { useEffect, useState } from "react";
import {
  commitSessionWorkspace, getIntegration, getIntegrationFile,
  resolveIntegrationFile, runIntegration
} from "../api/agui";
import type { IntegrationFile, IntegrationState } from "../types";

export function ProjectIntegrationPanel({ sessionId, targetBranch, shared, running, onChanged }: {
  sessionId: string;
  targetBranch?: string | null;
  shared: boolean;
  running: boolean;
  onChanged: () => void;
}) {
  const [state, setState] = useState<IntegrationState>({ status: "idle", conflicts: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [file, setFile] = useState<IntegrationFile | null>(null);
  const [draft, setDraft] = useState("");
  const disabled = busy || running;
  const active = !["idle", "completed", "aborted"].includes(state.status);

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const result = await getIntegration(sessionId);
        if (!cancelled) setState(result);
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "读取合并状态失败");
      }
    }
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 8000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [sessionId]);

  async function perform(action: "start" | "continue" | "abort" | "commit") {
    setBusy(true);
    setError("");
    try {
      if (action === "commit") {
        await commitSessionWorkspace(sessionId, message.trim());
        setMessage("");
      } else {
        setState(await runIntegration(sessionId, action));
        setFile(null);
      }
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
      try { setState(await getIntegration(sessionId)); } catch { /* 保留原错误供用户处理。 */ }
    } finally {
      setBusy(false);
    }
  }

  async function openConflict(path: string) {
    setBusy(true);
    setError("");
    try {
      const result = await getIntegrationFile(sessionId, path);
      setFile(result);
      setDraft(result.content);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取冲突失败");
    } finally { setBusy(false); }
  }

  async function resolve(choice: "manual" | "ours" | "theirs" | "delete") {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      setState(await resolveIntegrationFile(sessionId, file, choice, draft));
      setFile(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "解决冲突失败");
    } finally { setBusy(false); }
  }

  return (
    <section className="project-integration" aria-label="合入目标分支">
      <header><strong>目标分支 · {active ? state.targetBranch : targetBranch || "尚未设置"}</strong></header>
      {shared && <small>共享项目目录：提交会包含此目录中所有会话的改动。</small>}
      {!active && <>
        <div className="integration-commit">
          <input aria-label="Git 提交说明" placeholder="提交说明" value={message} disabled={disabled} onChange={(event) => setMessage(event.target.value)} />
          <button type="button" disabled={disabled || !message.trim()} onClick={() => void perform("commit")}>提交全部改动</button>
        </div>
        <button type="button" disabled={disabled || !targetBranch} onClick={() => void perform("start")}>合入 {targetBranch || "目标分支"}</button>
      </>}
      {state.message && <p role="status">{state.message}</p>}
      {active && <>
        <ul className="integration-conflicts">
          {state.conflicts.map((path) => <li key={path}><button type="button" disabled={disabled} onClick={() => void openConflict(path)}>{path}</button></li>)}
        </ul>
        {file && <div className="integration-editor">
          <strong>{file.path}</strong>
          {file.binary ? <p>二进制或大文件，请保留一方版本或删除。</p> : <textarea aria-label={`编辑冲突 ${file.path}`} spellCheck={false} value={draft} disabled={disabled} onChange={(event) => setDraft(event.target.value)} />}
          <div className="integration-actions">
            {!file.binary && <button type="button" disabled={disabled} onClick={() => void resolve("manual")}>保存解决结果</button>}
            <button type="button" disabled={disabled} onClick={() => void resolve("ours")}>保留目标分支</button>
            <button type="button" disabled={disabled} onClick={() => void resolve("theirs")}>保留会话版本</button>
            <button type="button" disabled={disabled} onClick={() => void resolve("delete")}>删除该文件</button>
          </div>
        </div>}
        <div className="integration-actions">
          <button type="button" disabled={disabled || state.conflicts.length > 0 || state.status === "failed"} onClick={() => void perform("continue")}>继续合入</button>
          <button type="button" disabled={disabled} onClick={() => void perform("abort")}>取消合并并丢弃本次冲突编辑</button>
        </div>
      </>}
      {error && <p role="alert" className="integration-error">{error}</p>}
    </section>
  );
}
