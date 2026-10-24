"""Session 成果汇总：在隔离目录解决合并，再以旧 OID 校验发布到目标分支。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from access_layer.home import projects_dir
from access_layer.projects.git import _git, GitWorkspaceError, inspect_repository, repository_lock
from access_layer.projects.models import ProjectRecord
from access_layer.schemas import SessionEnvironment
from access_layer.storage import write_json_atomic


def _directory(project: ProjectRecord, session_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise GitWorkspaceError("非法会话 ID")
    return projects_dir() / project.id / "integrations" / session_id


def _save(project: ProjectRecord, session_id: str, state: dict) -> dict:
    write_json_atomic(_directory(project, session_id) / "state.json", state)
    return state


def read_integration(project: ProjectRecord, session_id: str) -> dict:
    path = _directory(project, session_id) / "state.json"
    if not path.is_file():
        return {"status": "idle", "conflicts": [], "targetBranch": project.target_branch}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["status"] == "conflicts":
        workspace = _workspace(project, session_id, state)
        state["conflicts"] = _git(workspace, "diff", "--name-only", "--diff-filter=U", "-z").stdout.rstrip("\0").split("\0")
        state["conflicts"] = [path for path in state["conflicts"] if path]
    return state


def _workspace(project: ProjectRecord, session_id: str, state: dict) -> Path:
    root = _directory(project, session_id).resolve()
    path = Path(state["workspace"]).resolve()
    if path.parent != root or not path.name.startswith("workspace-"):
        raise GitWorkspaceError("合并目录越界")
    info = inspect_repository(path)
    if info.git_common_dir != state["gitCommonDir"]:
        raise GitWorkspaceError("合并目录的 Git 仓库已变化")
    return path


def target_checkout(root: Path, branch: str) -> Path | None:
    """Git porcelain -z 保留含空格/换行的路径，不解析本地化输出。"""
    current = None
    for entry in _git(root, "worktree", "list", "--porcelain", "-z").stdout.split("\0"):
        if entry.startswith("worktree "):
            current = Path(entry.removeprefix("worktree "))
        elif entry == f"branch refs/heads/{branch}":
            return current
    return None


def _inflight(root: Path) -> None:
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        location = Path(_git(root, "rev-parse", "--git-path", name).stdout.strip())
        if not location.is_absolute():
            location = root / location
        if location.exists():
            raise GitWorkspaceError("工作区正在执行其他 Git 操作，请先完成该操作")


def _clean(root: Path) -> None:
    _inflight(root)
    if _git(root, "status", "--porcelain", "--untracked-files=all").stdout:
        raise GitWorkspaceError("工作区有未提交改动，请先提交；不会自动覆盖或暂存用户改动")


def _root(project: ProjectRecord) -> Path:
    info = inspect_repository(project.root_path)
    if not info.repository_root:
        raise GitWorkspaceError("项目不是 Git 仓库")
    return Path(info.repository_root)


def integration_lock_paths(project: ProjectRecord, session_id: str, environment: SessionEnvironment) -> list[str]:
    root = _root(project)
    state = read_integration(project, session_id)
    branch = state.get("targetBranch") if state["status"] not in {"idle", "completed", "aborted"} else project.target_branch
    checkout = target_checkout(root, branch) if branch else None
    paths = {str(Path(environment.cwd).resolve()), str(Path(project.root_path).resolve())}
    if checkout:
        paths.add(str(checkout.resolve()))
    return sorted(paths)


def start_integration(project: ProjectRecord, session_id: str, environment: SessionEnvironment) -> dict:
    root = _root(project)
    info = inspect_repository(root)
    source = inspect_repository(environment.cwd)
    if source.git_common_dir != info.git_common_dir or not source.head_oid:
        raise GitWorkspaceError("会话工作区与项目仓库不一致")
    with repository_lock(info.git_common_dir):
        previous = read_integration(project, session_id)
        if previous["status"] not in {"idle", "completed", "aborted"}:
            return previous
        branch = project.target_branch
        if not branch:
            raise GitWorkspaceError("请先在项目设置中选择目标分支")
        _git(root, "check-ref-format", "--branch", branch)
        target_oid = _git(root, "rev-parse", "--verify", f"refs/heads/{branch}").stdout.strip()
        _clean(Path(source.repository_root))
        state = {
            "status": "completed", "sessionId": session_id, "targetBranch": branch,
            "sourceOid": source.head_oid, "targetOid": target_oid,
            "gitCommonDir": info.git_common_dir, "conflicts": [], "message": "成果已在目标分支中",
        }
        if _git(root, "merge-base", "--is-ancestor", source.head_oid, target_oid, check=False).returncode == 0:
            return _save(project, session_id, state)
        checkout = target_checkout(root, branch)
        if checkout:
            _clean(checkout)
        workspace = _directory(project, session_id) / f"workspace-{uuid.uuid4().hex}"
        workspace.parent.mkdir(parents=True, exist_ok=True)
        state.update(status="merging", workspace=str(workspace), message="正在合并")
        # 先保存固定的 source/target OID，应用重启后继续同一操作，不重新猜测分支。
        _save(project, session_id, state)
        _git(root, "worktree", "add", "--detach", str(workspace), target_oid)
        result = _git(workspace, "-c", "core.hooksPath=/dev/null", "-c", "user.name=K Agent", "-c", "user.email=k-agent@localhost", "merge", "--no-ff", "--no-commit", "--no-edit", source.head_oid, check=False)
        conflicts = [path for path in _git(workspace, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if path]
        if conflicts:
            state.update(status="conflicts", conflicts=conflicts, message="请解决冲突，再继续合入")
            return _save(project, session_id, state)
        if result.returncode != 0:
            state.update(status="failed", message=result.stderr.strip() or "Git 合并失败")
            return _save(project, session_id, state)
        state.update(status="ready", message="合并成功，正在更新目标分支")
        _save(project, session_id, state)
        return continue_integration(project, session_id)


def continue_integration(project: ProjectRecord, session_id: str) -> dict:
    state = read_integration(project, session_id)
    if state["status"] in {"completed", "aborted", "idle"}:
        return state
    root = _root(project)
    with repository_lock(state["gitCommonDir"]):
        workspace = _workspace(project, session_id, state)
        conflicts = [path for path in _git(workspace, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if path]
        if conflicts:
            state.update(status="conflicts", conflicts=conflicts)
            return _save(project, session_id, state)
        if not state.get("resultOid"):
            merge_head = Path(_git(workspace, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip())
            if not merge_head.is_absolute():
                merge_head = workspace / merge_head
            if merge_head.exists():
                _git(workspace, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=K Agent", "-c", "user.email=k-agent@localhost", "commit", "-m", f"Merge session {session_id} into {state['targetBranch']}")
            else:
                # merge commit 写入成功但 state.json 尚未写入时崩溃，可从双亲提交恢复。
                parents = _git(workspace, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()[1:]
                if parents != [state["targetOid"], state["sourceOid"]]:
                    raise GitWorkspaceError("没有可继续的合并，请取消后重新合入")
            state["resultOid"] = _git(workspace, "rev-parse", "HEAD").stdout.strip()
            state.update(status="ready", conflicts=[])
            _save(project, session_id, state)
        ref = f"refs/heads/{state['targetBranch']}"
        current = _git(root, "rev-parse", "--verify", ref).stdout.strip()
        if current != state["resultOid"]:
            if current != state["targetOid"]:
                raise GitWorkspaceError("目标分支已经更新，请取消本次合并后重新合入；已解决的文件仍保留在合并目录中")
            checkout = target_checkout(root, state["targetBranch"])
            if checkout:
                _clean(checkout)
                if _git(checkout, "symbolic-ref", "HEAD").stdout.strip() != ref:
                    raise GitWorkspaceError("目标 checkout 的分支已变化")
            state["status"] = "publishing"
            _save(project, session_id, state)
            if checkout:
                _git(checkout, "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", state["resultOid"])
            else:
                # 未被检出的目标分支使用 CAS；拒绝覆盖预检后外部新增的提交。
                _git(root, "update-ref", ref, state["resultOid"], state["targetOid"])
        state.update(status="completed", conflicts=[], message="已合入目标分支")
        _save(project, session_id, state)
        _git(root, "worktree", "remove", str(workspace), check=False)
        return state


def abort_integration(project: ProjectRecord, session_id: str) -> dict:
    state = read_integration(project, session_id)
    if state["status"] in {"idle", "completed", "aborted"}:
        return state
    root = _root(project)
    with repository_lock(state["gitCommonDir"]):
        current = _git(root, "rev-parse", "--verify", f"refs/heads/{state['targetBranch']}").stdout.strip()
        if state.get("resultOid") == current:
            state.update(status="completed", conflicts=[], message="目标分支已完成更新")
        else:
            state.update(status="aborted", conflicts=[], message="已取消，源会话与目标分支均保留")
        path = Path(state["workspace"])
        if path.exists():
            workspace = _workspace(project, session_id, state)
            # 用户点击取消仅丢弃本次冲突解决目录，不删除 Session 原 Worktree。
            _git(root, "worktree", "remove", "--force", str(workspace))
        return _save(project, session_id, state)


def conflict_file(project: ProjectRecord, session_id: str, relative: str) -> dict:
    state = read_integration(project, session_id)
    if state["status"] != "conflicts" or relative not in state["conflicts"]:
        raise GitWorkspaceError("文件不在当前冲突列表中")
    workspace = _workspace(project, session_id, state)
    candidate = workspace / relative
    if candidate.is_absolute() and (candidate.parent.resolve() != workspace and workspace not in candidate.parent.resolve().parents):
        raise GitWorkspaceError("不允许读取越界路径")
    # 符号链接仅显示/比较链接文本，绝不跟随到仓库外；选择一方或删除仍可解决。
    raw = ("symlink:" + candidate.readlink().as_posix()).encode() if candidate.is_symlink() else candidate.read_bytes() if candidate.is_file() else b""
    binary = candidate.is_symlink() or b"\0" in raw or len(raw) > 1024 * 1024
    try:
        content = raw.decode("utf-8") if not binary else ""
    except UnicodeDecodeError:
        content, binary = "", True
    return {"path": relative, "content": content, "binary": binary, "hash": hashlib.sha256(raw).hexdigest()}


def resolve_conflict(project: ProjectRecord, session_id: str, payload: dict) -> dict:
    relative = payload["path"]
    state = read_integration(project, session_id)
    with repository_lock(state["gitCommonDir"]):
        current = conflict_file(project, session_id, relative)
        if current["hash"] != payload["expectedHash"]:
            raise GitWorkspaceError("冲突文件已变化，请重新加载后编辑")
        workspace = _workspace(project, session_id, state)
        choice = payload["choice"]
        if choice == "delete":
            _git(workspace, "rm", "--", relative)
        else:
            if choice in {"ours", "theirs"}:
                _git(workspace, "checkout", f"--{choice}", "--", relative)
            else:
                if current["binary"]:
                    raise GitWorkspaceError("二进制冲突请保留一方或删除")
                content = payload.get("content", "")
                if re.search(r"^(<<<<<<< |=======$|>>>>>>> )", content, re.MULTILINE):
                    raise GitWorkspaceError("文件仍包含冲突标记")
                (workspace / relative).write_text(content, encoding="utf-8")
            _git(workspace, "add", "--", relative)
        state["conflicts"] = [path for path in state["conflicts"] if path != relative]
        return _save(project, session_id, state)


def commit_workspace(environment: SessionEnvironment, message: str) -> str:
    """显式的“提交全部改动”操作，用户目录中未跟踪且未忽略的文件也会提交。"""
    info = inspect_repository(environment.cwd)
    if not info.repository_root or info.git_common_dir != environment.git_common_dir:
        raise GitWorkspaceError("工作区仓库已变化")
    root = Path(info.repository_root)
    with repository_lock(info.git_common_dir):
        _inflight(root)
        if not _git(root, "status", "--porcelain", "--untracked-files=all").stdout:
            return info.head_oid or ""
        _git(root, "add", "--all")
        _git(root, "commit", "-m", message)
        return _git(root, "rev-parse", "HEAD").stdout.strip()
