import { useState, type ReactNode } from "react";
import type { ProjectSummary, SessionSummary } from "../types";
import { selectSidebarSessions } from "../sidebar-sessions";

const PREVIEW_LIMIT = 5;

/** 置顶目录在原位展开；更多会话使用区域内滚动，不挤占下方最近列表。 */
export function PinnedProjects({ projects, sessions, selectedSessionId, onBrowse, onOpenSession, onUnpin, renderProject }: {
  projects: ProjectSummary[];
  sessions: SessionSummary[];
  selectedSessionId?: string | null;
  onBrowse: () => void;
  onOpenSession: (sessionId: string) => void;
  onUnpin: (projectId: string) => void;
  renderProject?: (project: ProjectSummary, expanded: boolean, onToggle: () => void) => ReactNode;
}) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [showAllIds, setShowAllIds] = useState<Set<string>>(() => new Set());
  function toggle(setter: typeof setExpandedIds, id: string) {
    setter((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  }
  return <section className="sidebar-pinned" aria-label="置顶项目">
    <div className="sidebar-projects-heading"><span>置顶</span><button type="button" className="project-group-action" aria-label="浏览项目目录" title="浏览项目目录" onClick={onBrowse}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 6V4.5h5L9 6h8.5v10h-15V6Zm0 2h15"/></svg></button></div>
    <div className="sidebar-projects-list">
      {projects.length === 0 && <button className="sidebar-project-empty" type="button" onClick={onBrowse}>从项目页添加置顶</button>}
      {projects.map((project) => {
        const expanded = expandedIds.has(project.id);
        const showAll = showAllIds.has(project.id);
        const projectSessions = selectSidebarSessions(sessions, "", project.id);
        const visible = showAll ? projectSessions : projectSessions.slice(0, PREVIEW_LIMIT);
        const onToggle = () => toggle(setExpandedIds, project.id);
        return <div className="pinned-project-group" key={project.id}>
          {renderProject ? renderProject(project, expanded, onToggle) : <div className="project-group-header">
            <button type="button" className="sidebar-project-select" aria-expanded={expanded} aria-label={`${expanded ? "收起" : "展开"} ${project.name} 的会话`} onClick={onToggle}>
              <svg className="pinned-folder-icon" viewBox="0 0 20 20" aria-hidden="true">{expanded ? <path d="M2.5 8V4.5h5L9 6h8.5v2m-15 0h15L16 16H4L2.5 8Z"/> : <path d="M2.5 6V4.5h5L9 6h8.5v10h-15V6Zm0 2h15"/>}</svg><span className="project-group-name">{project.name}</span>
            </button>
            <button type="button" className="project-group-action" aria-label={`取消置顶 ${project.name}`} title="取消置顶" onClick={() => onUnpin(project.id)}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 3 12 12M8 3h6l-1 5 3 3H9m-2 0H4l3-3M10 11v6"/></svg></button>
          </div>}
          {expanded && <div className="pinned-project-children">
            <div className={`pinned-session-list ${showAll ? "show-all" : ""}`} role="group" aria-label={`${project.name} 的置顶会话列表`}>
              {visible.map((session) => <button className={`pinned-session-button ${selectedSessionId === session.id ? "active" : ""}`} aria-current={selectedSessionId === session.id ? "page" : undefined} type="button" key={session.id} onClick={() => onOpenSession(session.id)} title={session.title}>{session.title}</button>)}
              {projectSessions.length === 0 && <p className="pinned-empty">还没有会话</p>}
            </div>
            {projectSessions.length > PREVIEW_LIMIT && <button className="pinned-show-more" type="button" aria-expanded={showAll} onClick={() => toggle(setShowAllIds, project.id)}>{showAll ? "收起更多" : `展开更多（${projectSessions.length - PREVIEW_LIMIT}）`}</button>}
          </div>}
        </div>;
      })}
    </div>
  </section>;
}
