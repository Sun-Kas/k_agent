import React, { useEffect, useRef, useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";
import type { ProjectSummary, SessionSummary } from "../../protocol/index.js";
import { TERMINAL_DESIGN } from "../design.js";
import { orderProjects, sessionMeta, sessionsInProject, startChoices, deleteSessionWarning, type ProjectStartChoice } from "../project-home.js";
import type { TerminalPageAction, TerminalPageViewModel } from "../types.js";

type Pane = "list" | "inside" | "browse" | "branch" | "delete" | "mkdir";
type InsideRow = { kind: "start"; choice: ProjectStartChoice } | { kind: "session"; session: SessionSummary };
type BrowseRow = { kind: "use" | "parent" | "dir"; label: string; path?: string };

/**
 * 项目首页：先选项目，再看该项目的会话和开工方式。
 * 目录浏览、目标分支和删除确认都留在这一页，避免退回通用表单。
 */
export function ProjectHomeScreen({ model, onAction }: {
  model: TerminalPageViewModel;
  onAction: (action: TerminalPageAction) => void;
}): React.ReactElement {
  const { stdout } = useStdout();
  const height = Math.max(6, (stdout.rows ?? 24) - 8);
  const [pane, setPane] = useState<Pane>(model.activeProjectId ? "inside" : "list");
  const [selected, setSelected] = useState(0);
  const [filter, setFilter] = useState("");
  const [folderName, setFolderName] = useState("");
  const [pendingDelete, setPendingDelete] = useState<SessionSummary>();
  const seenProject = useRef(model.activeProjectId);
  useEffect(() => {
    if (model.activeProjectId && model.activeProjectId !== seenProject.current && pane === "browse") {
      setPane("inside");
      setSelected(0);
    }
    seenProject.current = model.activeProjectId;
  }, [model.activeProjectId, pane]);
  const projects = orderProjects(model.projects, model.pinnedProjectIds)
    .filter((item) => `${item.name} ${item.rootPath}`.toLowerCase().includes(filter.trim().toLowerCase()));
  const project = model.projects.find((item) => item.id === model.activeProjectId);
  const sessions = project ? sessionsInProject(model.sessions, project.id) : [];
  const starts = project ? startChoices(project) : [];
  const insideRows: InsideRow[] = [
    ...starts.map((choice) => ({ kind: "start" as const, choice })),
    ...sessions.map((session) => ({ kind: "session" as const, session })),
  ];
  // 没有分支快照时 projectId 也是 undefined，不能把它当成“当前项目已加载”。
  const branches = model.projectBranches && model.projectBranches.projectId === project?.id ? model.projectBranches.branches : [];
  const branchRows = ["", ...branches];
  const listing = model.directory;
  const browseRows: BrowseRow[] = listing ? [
    { kind: "use", label: `使用此目录  ${listing.path}` },
    ...(listing.parent ? [{ kind: "parent" as const, label: "..", path: listing.parent }] : []),
    ...listing.entries.map((entry) => ({ kind: "dir" as const, label: entry.name, path: entry.path })),
  ] : [];
  const count = pane === "list" ? projects.length
    : pane === "inside" ? insideRows.length
      : pane === "branch" ? branchRows.length
        : pane === "browse" ? browseRows.length
          : 1;
  const index = Math.min(selected, Math.max(0, count - 1));

  useInput((input, key) => {
    if (key.ctrl || key.meta) return;
    if (input === "?" && !filter && pane === "list") return;
    if (pane === "mkdir") {
      if (key.escape) return setPane("browse");
      if (key.backspace || key.delete) return setFolderName((value) => value.slice(0, -1));
      if (key.return) {
        if (!listing || !folderName.trim()) return;
        onAction({ type: "make_directory", parentPath: listing.path, name: folderName.trim() });
        setFolderName("");
        setPane("browse");
        return;
      }
      if (input) setFolderName((value) => value + input);
      return;
    }
    if (key.escape) {
      if (pane === "delete") return setPane("inside");
      if (pane === "branch" || pane === "browse") return setPane(project ? "inside" : "list");
      if (pane === "inside") {
        setPane("list");
        setSelected(0);
        onAction({ type: "clear_project" });
        return;
      }
      onAction({ type: "open_surface", surface: "chat" });
      return;
    }
    if (pane === "delete") {
      if (key.return && pendingDelete) {
        onAction({ type: "delete_session", sessionId: pendingDelete.id });
        setPane("inside");
      }
      return;
    }
    if (pane === "list" && !key.upArrow && !key.downArrow && !key.return && !key.tab) {
      if (key.backspace || key.delete) return setFilter((value) => value.slice(0, -1));
      if (input === "+" && !filter) {
        setPane("browse");
        setSelected(0);
        onAction({ type: "browse_directory" });
        return;
      }
      if (input === "p" && !filter && projects[index]) {
        onAction({ type: "pin_project", projectId: projects[index]!.id });
        return;
      }
      if (input && !key.return) {
        setFilter((value) => value + input);
        setSelected(0);
      }
      return;
    }
    if (key.upArrow) return setSelected((value) => (value + count - 1) % Math.max(1, count));
    if (key.downArrow) return setSelected((value) => (value + 1) % Math.max(1, count));
    if (!key.return) {
      if (pane === "inside" && project) return insideCommand(input, project, insideRows[index]);
      if (pane === "browse" && input === "+" && listing) return setPane("mkdir");
      return;
    }
    if (pane === "list" && projects[index]) {
      setPane("inside");
      setSelected(0);
      setFilter("");
      onAction({ type: "enter_project", projectId: projects[index]!.id });
      return;
    }
    if (pane === "inside") {
      const row = insideRows[index];
      if (!project || !row) return;
      if (row.kind === "start") {
        onAction({ type: "start_project_session", projectId: project.id, environment: row.choice.environment, startingState: row.choice.startingState });
      } else {
        onAction({ type: "select_session", sessionId: row.session.id });
      }
      return;
    }
    if (pane === "branch" && project) {
      const branch = branchRows[index] ?? "";
      onAction({ type: "set_target_branch", projectId: project.id, name: project.name, targetBranch: branch || null });
      setPane("inside");
      return;
    }
    if (pane === "browse") {
      const row = browseRows[index];
      if (!row) return;
      if (row.kind === "use" && listing) onAction({ type: "create_project", rootPath: listing.path });
      else if (row.path) {
        setSelected(0);
        onAction({ type: "browse_directory", path: row.path });
      }
    }
  });

  function insideCommand(input: string, current: ProjectSummary, row: InsideRow | undefined): void {
    if (input === "p") return onAction({ type: "pin_project", projectId: current.id });
    if (input === "e" && current.isGitRepository) {
      setPane("branch");
      setSelected(0);
      onAction({ type: "load_branches", projectId: current.id });
      return;
    }
    if (!row || row.kind !== "session") return;
    const running = model.timeline.runStatus === "running" && model.activeSessionId === row.session.id;
    if (running) return;
    if (input === "b") onAction({ type: "fork_session", sessionId: row.session.id, environment: "same-directory" });
    if (input === "i" && row.session.environment?.repositoryRoot) onAction({ type: "fork_session", sessionId: row.session.id, environment: "worktree" });
    if (input === "m" && !row.session.projectId) onAction({ type: "bind_session", sessionId: row.session.id, projectId: current.id });
    if (input === "d") {
      setPendingDelete(row.session);
      setPane("delete");
    }
  }

  const start = Math.max(0, Math.min(index - Math.floor(height / 2), count - height));
  return (
    <Box flexDirection="column" paddingX={1}>
      <Text bold color={TERMINAL_DESIGN.colors.accent}>{paneTitle(pane, project)}</Text>
      {pane === "list" ? <Text color={TERMINAL_DESIGN.colors.muted}>Enter 进入 · p 置顶 · + 新建 · 输入筛选 · Esc 返回对话</Text> : null}
      {pane === "inside" ? <Text color={TERMINAL_DESIGN.colors.muted}>Enter 打开 · b 同目录分支 · i 隔离分支 · d 删除 · e 目标分支 · Esc 所有项目</Text> : null}
      {pane === "browse" ? <Text color={TERMINAL_DESIGN.colors.muted}>Enter 进入或使用当前目录 · + 新建文件夹 · Esc 返回</Text> : null}
      {pane === "branch" ? <Text color={TERMINAL_DESIGN.colors.muted}>选择会话改动要合入的分支 · Esc 返回</Text> : null}
      {pane === "mkdir" ? <Text>新文件夹名称：{folderName || "…"} · Enter 创建 · Esc 取消</Text> : null}
      {pane === "delete" && pendingDelete ? (
        <Box flexDirection="column">
          <Text>删除「{pendingDelete.title || "未命名会话"}」？</Text>
          <Text color={TERMINAL_DESIGN.colors.warning}>{deleteSessionWarning(pendingDelete)}</Text>
          <Text>Enter 确认 · Esc 取消</Text>
        </Box>
      ) : null}
      {pane === "list" ? projects.slice(start, start + height).map((item, row) => (
        <ProjectRow key={item.id} project={item} pinned={model.pinnedProjectIds.includes(item.id)} selected={start + row === index} />
      )) : null}
      {pane === "list" && projects.length === 0 ? <Text color={TERMINAL_DESIGN.colors.muted}>{filter ? "没有匹配的项目" : "还没有项目。按 + 选择一个本地文件夹。"}</Text> : null}
      {pane === "inside" && project ? (
        <Text color={TERMINAL_DESIGN.colors.muted} wrap="truncate-end">{project.rootPath}{project.isGitRepository ? ` · 合入 ${project.targetBranch || "跟随当前分支"}` : " · 本地目录"} · {sessions.length} 个会话</Text>
      ) : null}
      {pane === "inside" ? insideRows.slice(start, start + height).map((row, offset) => (
        <Text key={row.kind === "start" ? row.choice.id : row.session.id} inverse={start + offset === index} wrap="truncate-end">
          {start + offset === index ? "›" : " "} {row.kind === "start" ? row.choice.label : `${row.session.title || "未命名会话"} · ${sessionMeta(row.session, model.projects)}`}
        </Text>
      )) : null}
      {pane === "branch" ? branchRows.slice(start, start + height).map((branch, offset) => (
        <Text key={branch || "follow"} inverse={start + offset === index}>{start + offset === index ? "›" : " "} {branch || "跟随当前分支"}{(project?.targetBranch ?? "") === branch ? "  ✓" : ""}</Text>
      )) : null}
      {pane === "browse" && !listing ? <Text color={TERMINAL_DESIGN.colors.muted}>正在读取目录…</Text> : null}
      {pane === "browse" ? browseRows.slice(start, start + height).map((row, offset) => (
        <Text key={`${row.kind}:${row.path ?? row.label}`} inverse={start + offset === index} wrap="truncate-end">{start + offset === index ? "›" : " "} {row.label}</Text>
      )) : null}
    </Box>
  );
}

function ProjectRow({ project, pinned, selected }: { project: ProjectSummary; pinned: boolean; selected: boolean }): React.ReactElement {
  return (
    <Text inverse={selected} wrap="truncate-end">
      {selected ? "›" : " "} {pinned ? "★ " : ""}{project.name}  {project.rootPath}  {project.sessionCount} 个会话{project.isGitRepository ? ` · ${project.targetBranch || "跟随当前分支"}` : ""}
    </Text>
  );
}

function paneTitle(pane: Pane, project: ProjectSummary | undefined): string {
  if (pane === "browse" || pane === "mkdir") return "新建项目";
  if (pane === "branch") return `${project?.name ?? "项目"} · 目标分支`;
  if (pane === "delete") return "删除会话";
  if (pane === "inside") return project?.name ?? "项目";
  return "项目";
}
