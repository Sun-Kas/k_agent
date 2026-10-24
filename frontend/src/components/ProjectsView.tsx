import type { ProjectSummary } from "../types";

export function ProjectsView({ projects, pinnedIds, onTogglePin, busyId, error, onCreate, onEdit, onOpen, onNewSession, onToggleSidebar }: {
  projects: ProjectSummary[];
  pinnedIds: string[];
  onTogglePin: (projectId: string) => void;
  busyId: string | null;
  error: { id: string; message: string } | null;
  onCreate: () => void;
  onEdit: (project: ProjectSummary) => void;
  onOpen: (projectId: string) => void;
  onNewSession: (projectId: string) => void;
  onToggleSidebar: () => void;
}) {
  return <section className="projects-page" aria-label="项目管理">
    <header className="projects-page-heading">
      <button type="button" className="topbar-icon-button" onClick={onToggleSidebar} aria-label="切换会话侧栏">
        <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3" y="4" width="14" height="12" rx="2"/><path d="M8 4v12"/></svg>
      </button>
      <h1>项目</h1>
      <button className="projects-create-button" type="button" onClick={onCreate}><span aria-hidden="true">＋</span>新建项目</button>
    </header>
    <div className="projects-page-body">
      <p className="projects-page-intro">管理项目目录，继续相关会话。</p>
      {projects.length === 0 ? <div className="projects-empty"><p>还没有项目</p><span>选择一个本地文件夹，开始你的第一个项目。</span><button type="button" onClick={onCreate}>新建项目</button></div> : (
        <div className="projects-directory">
          {projects.map((project) => <article className="projects-directory-row" key={project.id}>
            <svg className="projects-directory-icon" viewBox="0 0 20 20" aria-hidden="true"><path d="M2.5 6V4.5h5L9 6h8.5v10h-15V6Zm0 2h15"/></svg>
            <button type="button" className="projects-directory-info" onClick={() => onOpen(project.id)}>
              <strong>{project.name}</strong><span>{project.rootPath}</span>
              <small>{project.sessionCount} 个会话{project.isGitRepository ? ` · ${project.targetBranch || "跟随当前分支"}` : " · 本地目录"}</small>
            </button>
            <div className="projects-directory-actions">
              <button className={pinnedIds.includes(project.id) ? "is-pinned" : ""} type="button" aria-pressed={pinnedIds.includes(project.id)}
                aria-label={`${pinnedIds.includes(project.id) ? "取消置顶" : "置顶"} ${project.name}`} title={pinnedIds.includes(project.id) ? "取消置顶" : "添加到侧栏置顶"} onClick={() => onTogglePin(project.id)}>
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 3h6l-1 5 3 3v1H5v-1l3-3-1-5Zm3 9v5"/></svg>
              </button>
              <button type="button" aria-label={`编辑 ${project.name}`} title="编辑项目" onClick={() => onEdit(project)}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h.01M10 10h.01M16 10h.01" strokeWidth="3"/></svg></button>
              <button type="button" disabled={busyId === project.id} aria-label={`在 ${project.name} 下新建对话`} title="新建对话" onClick={() => onNewSession(project.id)}><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M8 4H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-3M12.5 3.5a2.1 2.1 0 0 1 3 3L9 13l-4 1 1-4Z"/></svg></button>
            </div>
            {error?.id === project.id && <p className="session-action-error" role="alert">{error.message}</p>}
          </article>)}
        </div>
      )}
    </div>
  </section>;
}
