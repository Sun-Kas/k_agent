"""Project 的 Git 仓库探测、托管 worktree 与只读变更查询。"""

from __future__ import annotations

import subprocess
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from access_layer.home import session_worktree_dir
from access_layer.projects.models import ProjectRecord, RepositoryInfo, ProjectSessionCreateInput
from access_layer.schemas import SessionEnvironment

_repository_locks: dict[str, threading.RLock] = {}
_repository_locks_guard = threading.Lock()


def repository_lock(common_dir: str) -> threading.RLock:
    """同一 common-dir 的 Git 元数据操作串行；工具运行不持有此锁。"""
    with _repository_locks_guard:
        return _repository_locks.setdefault(common_dir, threading.RLock())


class GitWorkspaceError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=check,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            input=input,
            timeout=20,
        )
    except subprocess.CalledProcessError as exc:
        raise GitWorkspaceError((exc.stderr or str(exc)).strip()) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitWorkspaceError(str(exc)) from exc


def inspect_repository(path: str | Path) -> RepositoryInfo:
    """按 Git common-dir 识别仓库；普通目录和未初始化目录返回非 Git。"""

    cwd = Path(path).expanduser().resolve()
    top = _git(cwd, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        return RepositoryInfo(isGitRepository=False)
    root = Path(top.stdout.strip()).resolve()
    common = _git(root, "rev-parse", "--git-common-dir").stdout.strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = root / common_path
    head = _git(root, "rev-parse", "--verify", "HEAD", check=False)
    branch = _git(root, "symbolic-ref", "-q", "HEAD", check=False)
    branch_ref = branch.stdout.strip() or None
    return RepositoryInfo(
        isGitRepository=True,
        repositoryRoot=str(root),
        gitCommonDir=str(common_path.resolve()),
        headOid=head.stdout.strip() if head.returncode == 0 else None,
        branchRef=branch_ref,
        detached=branch_ref is None and head.returncode == 0,
    )


def prepare_project_session(project: ProjectRecord, session_id: str, options: ProjectSessionCreateInput) -> SessionEnvironment:
    """解析创建入口的 startingState，避免调用方忽略用户指定的起始状态。"""
    state = options.starting_state
    root = Path(project.root_path)
    if options.environment == "local" and state.type != "default":
        raise GitWorkspaceError("本地会话使用当前 checkout，不能指定新的起始状态")
    copy_from = None
    start_ref = options.start_ref
    info = inspect_repository(root)
    if options.environment in {"auto", "local"}:
        if state.type != "default" or options.start_ref is not None:
            raise GitWorkspaceError("指定起始状态需要显式选择独立 Worktree")
        return prepare_session_environment(project, session_id, "local")
    if state.type == "default" and project.target_branch and start_ref is None:
        start_ref = f"refs/heads/{project.target_branch}"
    if state.type == "default" and start_ref is None and info.head_oid:
        remote_default = _git(root, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).stdout.strip()
        candidates = []
        if remote_default.startswith("refs/remotes/origin/"):
            candidates.append("refs/heads/" + remote_default.removeprefix("refs/remotes/origin/"))
        candidates.extend(["refs/heads/main", "refs/heads/master", "HEAD"])
        for candidate in candidates:
            if _git(root, "rev-parse", "--verify", "--end-of-options", candidate, check=False).returncode == 0:
                start_ref = candidate
                break
    if state.type == "working-tree":
        if not info.head_oid or not info.repository_root:
            raise GitWorkspaceError("复制工作树需要已有 Git 提交")
        start_ref = info.head_oid
        copy_from = info.repository_root
    elif state.type == "branch":
        if not state.branch_name or not info.repository_root:
            raise GitWorkspaceError("必须指定 Git 分支名称")
        _git(root, "check-ref-format", "--branch", state.branch_name)
        start_ref = f"refs/heads/{state.branch_name}"
        exists = _git(root, "show-ref", "--verify", "--quiet", start_ref, check=False)
        if exists.returncode != 0:
            if state.on_missing != "create-branch":
                raise GitWorkspaceError("指定分支不存在")
            with repository_lock(info.git_common_dir or str(root)):
                _git(root, "branch", "--", state.branch_name)
    return prepare_session_environment(
        project, session_id, options.environment, start_ref, copy_from,
        state.branch_name if state.type == "branch" else None,
    )


def prepare_session_environment(
    project: ProjectRecord,
    session_id: str,
    requested: str = "auto",
    start_ref: str | None = None,
    copy_from: str | None = None,
    attach_branch: str | None = None,
) -> SessionEnvironment:
    """auto/local 共享项目目录，仅显式 worktree 创建隔离工作树。"""

    info = inspect_repository(project.root_path)
    if requested in {"auto", "local"} or not info.is_git_repository:
        if requested == "worktree" and not info.is_git_repository:
            raise GitWorkspaceError("This Project directory is not a Git repository")
        return SessionEnvironment(
            kind="local",
            cwd=project.root_path,
            projectId=project.id,
            repositoryRoot=info.repository_root,
            gitCommonDir=info.git_common_dir,
            baseOid=info.head_oid,
            branchRef=info.branch_ref,
            detached=info.detached,
        )
    if info.head_oid is None or info.repository_root is None:
        if requested == "worktree":
            raise GitWorkspaceError("Git repository has no commit to create a worktree from")
        return SessionEnvironment(
            kind="local",
            cwd=project.root_path,
            projectId=project.id,
            repositoryRoot=info.repository_root,
            gitCommonDir=info.git_common_dir,
        )

    destination = session_worktree_dir(project.id, session_id).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise GitWorkspaceError(f"Managed worktree already exists: {destination}")
    ref = (start_ref or "HEAD").strip() or "HEAD"
    resolved = _git(Path(info.repository_root), "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    base_oid = resolved.stdout.strip()
    project_relative = Path(project.root_path).resolve().relative_to(Path(info.repository_root))
    with repository_lock(info.git_common_dir or info.repository_root):
        if attach_branch:
            _git(Path(info.repository_root), "worktree", "add", str(destination), attach_branch)
        else:
            _git(Path(info.repository_root), "worktree", "add", "--detach", str(destination), base_oid)
        if copy_from:
            try:
                copy_working_state(Path(copy_from), destination, base_oid)
            except BaseException:
                _git(Path(info.repository_root), "worktree", "remove", "--force", str(destination))
                raise
        if not (destination / project_relative).is_dir():
            _git(Path(info.repository_root), "worktree", "remove", "--force", str(destination))
            raise GitWorkspaceError("目标提交中不存在项目子目录")
    return SessionEnvironment(
        kind="worktree",
        cwd=str(destination / project_relative),
        projectId=project.id,
        repositoryRoot=info.repository_root,
        gitCommonDir=info.git_common_dir,
        baseOid=base_oid,
        branchRef=f"refs/heads/{attach_branch}" if attach_branch else None,
        detached=attach_branch is None,
        worktreeId=session_id,
    )


def copy_working_state(source: Path, destination: Path, expected_head: str) -> None:
    """复制 staged/unstaged/untracked 状态；复制期间源发生变化就拒绝该快照。"""
    if _git(source, "rev-parse", "HEAD").stdout.strip() != expected_head:
        raise GitWorkspaceError("源工作区提交已变化，请重试")
    staged = _git(source, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--cached", "HEAD").stdout
    unstaged = _git(source, "diff", "--binary", "--no-ext-diff", "--no-textconv").stdout
    untracked = _git(source, "ls-files", "--others", "--exclude-standard", "-z").stdout
    stamps: dict[str, tuple[int, int, int]] = {}
    for name in filter(None, untracked.split("\0")):
        path = source / name
        stat = path.lstat()
        stamps[name] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if staged:
        _git(destination, "apply", "--binary", "--index", "-", input=staged)
    if unstaged:
        _git(destination, "apply", "--binary", "-", input=unstaged)
    for name in stamps:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target, follow_symlinks=False)
    # 不对源 checkout 做 stash/reset，保留用户的索引和未提交工作。
    if (
        _git(source, "rev-parse", "HEAD").stdout.strip() != expected_head
        or _git(source, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--cached", "HEAD").stdout != staged
        or _git(source, "diff", "--binary", "--no-ext-diff", "--no-textconv").stdout != unstaged
        or _git(source, "ls-files", "--others", "--exclude-standard", "-z").stdout != untracked
        or any(
            not (source / name).exists() and not (source / name).is_symlink()
            or (lambda stat: (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))((source / name).lstat()) != stamp
            for name, stamp in stamps.items()
        )
    ):
        raise GitWorkspaceError("源工作区在复制期间变化，请重试")


def remove_managed_worktree(environment: SessionEnvironment) -> None:
    """仅清理明确标记为托管 worktree 的目录，用于创建会话失败后的回滚。"""

    if environment.kind != "worktree" or not environment.repository_root:
        return
    if not environment.worktree_id:
        raise GitWorkspaceError("Cannot remove a worktree without an ownership binding")
    destination = session_worktree_dir(environment.project_id, environment.worktree_id).resolve()
    cwd = Path(environment.cwd).resolve()
    if cwd != destination and destination not in cwd.parents:
        raise GitWorkspaceError("Cannot remove a workspace outside its managed path")
    with repository_lock(environment.git_common_dir or environment.repository_root):
        _git(
            Path(environment.repository_root), "worktree", "remove", "--force", str(destination)
        )


def handoff_environment(
    project: ProjectRecord, session_id: str, current: SessionEnvironment, target: str
) -> SessionEnvironment:
    """切换干净工作区，保留旧 worktree；返回本地时仅允许快进以保存提交。"""
    if current.kind == target:
        return current
    source = inspect_repository(current.cwd)
    if not source.is_git_repository or not source.head_oid or not source.git_common_dir:
        raise GitWorkspaceError("当前工作区没有可交接的 Git 提交")
    with repository_lock(source.git_common_dir):
        source_dirty = bool(_git(Path(current.cwd), "status", "--porcelain", "--untracked-files=all").stdout)
        if source_dirty and target == "local":
            raise GitWorkspaceError("当前工作区有未提交改动，请先提交后再切换")
        if target == "worktree":
            destination = session_worktree_dir(project.id, session_id).resolve()
            if destination.exists():
                if source_dirty:
                    raise GitWorkspaceError("已有保留的 Worktree，请先提交本地修改后再切换")
                retained = inspect_repository(destination)
                if retained.git_common_dir != source.git_common_dir:
                    raise GitWorkspaceError("保留的 Worktree 属于另一个仓库")
                if _git(destination, "status", "--porcelain", "--untracked-files=all").stdout:
                    raise GitWorkspaceError("保留的 Worktree 有未提交改动")
                if retained.head_oid != source.head_oid:
                    raise GitWorkspaceError("保留的 Worktree 与当前提交不同，请先在 Git 中合并")
                return current.model_copy(update={
                    "kind": "worktree",
                    "cwd": str(destination / Path(project.root_path).resolve().relative_to(Path(current.repository_root))),
                    "worktree_id": session_id, "branch_ref": retained.branch_ref,
                    "detached": retained.detached,
                })
            binding = prepare_session_environment(
                project, session_id, "worktree", source.head_oid,
                source.repository_root if source_dirty else None,
            )
        else:
            local = inspect_repository(project.root_path)
            if local.git_common_dir != source.git_common_dir:
                raise GitWorkspaceError("本地目录与当前 Worktree 不属于同一个仓库")
            local_root = Path(project.root_path)
            if _git(local_root, "status", "--porcelain", "--untracked-files=all").stdout:
                raise GitWorkspaceError("本地项目有未提交改动，请先提交后再切换")
            # 只接受快进，分叉历史交给显式合并；不使用 reset 覆盖本地工作内容。
            _git(local_root, "merge", "--ff-only", source.head_oid)
            binding = prepare_session_environment(project, session_id, "local")
        binding.base_oid = current.base_oid
        return binding


@dataclass(frozen=True, slots=True)
class GitChange:
    path: str
    status: str
    original_path: str | None = None


def list_changes(environment: SessionEnvironment) -> list[GitChange]:
    """读取 cwd 的工作树状态；不改索引，不触发 refresh 之外的 Git 写操作。"""

    if not environment.repository_root:
        return []
    cwd = Path(environment.cwd)
    prefix = _git(cwd, "rev-parse", "--show-prefix").stdout.strip()
    result = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
    tokens = result.stdout.split("\0")
    changes: list[GitChange] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        status = token[:2]
        path = token[3:]
        original = None
        if "R" in status or "C" in status:
            if index < len(tokens):
                original = tokens[index] or None
                index += 1
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
            if original and original.startswith(prefix):
                original = original[len(prefix):]
        changes.append(GitChange(path=path, status=status, original_path=original))
    # Review 基线包含会话期间已经提交的内容，不能只列 status 的未提交文件。
    if environment.base_oid:
        committed = _git(cwd, "diff", "--relative", "--name-status", "-z", environment.base_oid, "HEAD", "--", ".").stdout.split("\0")
        known = {change.path for change in changes}
        index = 0
        while index < len(committed) and committed[index]:
            status = committed[index]
            index += 1
            original = None
            path = committed[index]
            index += 1
            if status.startswith(("R", "C")):
                original = path
                path = committed[index]
                index += 1
            if path not in known:
                changes.append(GitChange(path=path, status=status, original_path=original))
                known.add(path)
    return changes


def diff_for_path(environment: SessionEnvironment, relative_path: str) -> str:
    root = Path(environment.cwd).resolve()
    candidate = (root / relative_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise GitWorkspaceError("Path escapes the session workspace")
    if candidate.is_file() and candidate.stat().st_size > 2 * 1024 * 1024:
        return "文件大于 2 MiB，省略文本差异。"
    base = environment.base_oid or "HEAD"
    # 已删除、已暂存删除的文件可能不在索引中，但仍必须对基线生成删除 diff。
    in_base = _git(root, "cat-file", "-e", f"{base}:{relative_path}", check=False)
    tracked = _git(root, "ls-files", "--error-unmatch", "--", relative_path, check=False)
    unborn = environment.base_oid is None and _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    if (unborn or tracked.returncode != 0 and in_base.returncode != 0) and candidate.is_file():
        result = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-index", "--", "/dev/null", relative_path, check=False)
        diff = result.stdout
    else:
        diff = _git(root, "diff", "--relative", "--no-ext-diff", "--no-textconv", base, "--", relative_path).stdout
    limit = 256 * 1024
    encoded = diff.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        return encoded[:limit].decode("utf-8", errors="replace") + "\n…（差异已截断）"
    return diff
