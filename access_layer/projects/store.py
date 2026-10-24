"""Project 索引存储：JSON + CAS，和 SessionStore/TeamStore 同一套持久化哲学。

在请求链路中的角色：`/api/projects*` 路由、SessionStore（项目归属会话落盘位置）、
gateway（cwd 解析）、Team 路由（workspaceDir 按 projectId 解析）共用这一份
内存索引 + 落盘 index.json。

服务边界：
- 本类只拥有 state/projects/index.json 这张「纯索引表」；Project 的真实内容
  （root_path 指向的用户目录）从不被本类写入或删除。
- 单 worker 假设与 SessionStore 一致：内存 dict 由 asyncio.Lock 保护，落盘用
  FileStorage 的 revision CAS 兜底跨进程/崩溃重启一致性。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from access_layer.home import agent_home
from access_layer.projects.git import inspect_repository, _git, GitWorkspaceError
from access_layer.projects.models import ProjectCreateInput, ProjectRecord, ProjectUpdateInput
from access_layer.storage import StorageBackend

logger = logging.getLogger("k_agent.access_layer.projects")

_INDEX_KEY = "projects/index.json"
_MAX_CAS_ATTEMPTS = 10


class ProjectValidationError(ValueError):
    """root_path 非法（相对路径、系统根、$K_AGENT_HOME 内外包含关系等）。"""


def _project_id_for(resolved_root: Path) -> str:
    """id = sha256(resolve(root_path).as_posix())[:16]：创建时算一次，此后不变。

    用路径算哈希而不是随机 UUID，换来的是「同一个 root_path 两次创建天然
    去重成同一个 id」，不需要额外的反查表。
    """

    digest = hashlib.sha256(resolved_root.as_posix().encode("utf-8")).hexdigest()
    return digest[:16]


def validate_and_prepare_root_path(raw_path: str) -> Path:
    """校验 root_path 并确保目录存在；拒绝过宽/危险路径。

    规则（对应技术方案 §7.2）：
    - 必须是绝对路径（不接受相对 cwd 的路径，避免两个进程 cwd 不同时产生歧义）。
    - 不能是文件系统根、用户 home 根或 $K_AGENT_HOME 本身——这些要么太宽，
      要么会和 Access Layer 自己的簿记目录产生危险的嵌套关系。
    - 不能和 $K_AGENT_HOME 互相包含（Project 的簿记始终独立于它管理的内容）。
    - 允许目录尚不存在（对应前端「新建空文件夹」），此时原子创建它；
      已存在则必须是目录，不能是文件。
    """

    text = (raw_path or "").strip()
    if not text:
        raise ProjectValidationError("rootPath is required")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ProjectValidationError("rootPath must be an absolute path")
    resolved = candidate.resolve()

    home_root = agent_home().resolve()
    user_home = Path.home().resolve()
    filesystem_root = Path(resolved.anchor).resolve()
    disallowed_exact = {filesystem_root, user_home, home_root}
    if resolved in disallowed_exact:
        raise ProjectValidationError(
            "rootPath must not be the filesystem root, the user home directory, or $K_AGENT_HOME"
        )
    if resolved == home_root or home_root in resolved.parents:
        raise ProjectValidationError("rootPath must not be inside $K_AGENT_HOME")
    if resolved in home_root.parents:
        raise ProjectValidationError("rootPath must not contain $K_AGENT_HOME")

    if resolved.exists() and not resolved.is_dir():
        raise ProjectValidationError("rootPath must point to a directory, not a file")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectValidationError(f"rootPath cannot be created or is not writable: {exc}") from exc
    return resolved


class ProjectStore:
    """内存索引 + state/projects/index.json 的 CAS 落盘。"""

    def __init__(self, storage: StorageBackend | None = None) -> None:
        self._storage = storage
        self._lock = asyncio.Lock()
        self._projects: dict[str, ProjectRecord] = {}
        self._revision = 0
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            if self._storage is not None:
                raw = await self._storage.read_json(_INDEX_KEY)
                if isinstance(raw, dict):
                    self._revision = int(raw.get("revision") or 0)
                    for item in raw.get("projects", []):
                        try:
                            record = ProjectRecord.model_validate(item)
                            self._projects[record.id] = record
                        except Exception:
                            logger.error("Skipping unreadable project record: %r", item, exc_info=True)
            self._loaded = True

    async def list(self) -> list[ProjectRecord]:
        await self._ensure_loaded()
        async with self._lock:
            records = sorted(
                self._projects.values(), key=lambda project: project.updated_at, reverse=True
            )
        return list(await asyncio.gather(*(self._repository_view(record) for record in records)))

    async def get(self, project_id: str) -> ProjectRecord | None:
        await self._ensure_loaded()
        async with self._lock:
            record = self._projects.get(project_id)
        return await self._repository_view(record) if record is not None else None

    async def _repository_view(self, record: ProjectRecord) -> ProjectRecord:
        """Git 状态以磁盘为准，兼容升级前索引及项目登记后才初始化 Git 的情况。"""
        repository = await asyncio.to_thread(inspect_repository, record.root_path)
        return record.model_copy(update={
            "is_git_repository": repository.is_git_repository,
            "repository_root": repository.repository_root,
            "git_common_dir": repository.git_common_dir,
            "target_branch": record.target_branch or (
                repository.branch_ref.removeprefix("refs/heads/") if repository.branch_ref else None
            ),
        })

    async def create(self, payload: ProjectCreateInput) -> ProjectRecord:
        """创建或复用已存在的 Project：同一 root_path 永远落到同一个 id。"""

        resolved_root = validate_and_prepare_root_path(payload.root_path)
        project_id = _project_id_for(resolved_root)
        await self._ensure_loaded()
        async with self._lock:
            existing = self._projects.get(project_id)
            if existing is not None:
                # root_path 天然去重：重复 create 不是错误，直接把已有记录给回去，
                # 前端"打开已有目录"时不需要先查一遍是否已经登记过。
                return existing
            now = datetime.now(timezone.utc)
            name = (payload.name or "").strip() or resolved_root.name or project_id
            # 登记 Project 不修改用户目录。Git、README、CLAUDE.md、Skill/MCP 配置
            # 都是显式动作；这里只探测已有仓库，让创建 Project 保持可逆。
            repository = await asyncio.to_thread(inspect_repository, resolved_root)
            record = ProjectRecord(
                id=project_id,
                name=name,
                rootPath=str(resolved_root),
                isGitRepository=repository.is_git_repository,
                repositoryRoot=repository.repository_root,
                gitCommonDir=repository.git_common_dir,
                targetBranch=(repository.branch_ref.removeprefix("refs/heads/") if repository.branch_ref else None),
                sessionCount=0,
                createdAt=now,
                updatedAt=now,
                revision=0,
            )
            next_map = {**self._projects, project_id: record}
            await self._commit_locked(next_map)
            self._projects = next_map
            return record

    async def update(self, project_id: str, payload: ProjectUpdateInput) -> ProjectRecord | None:
        """目标分支只保存指向，不切换或改写当前用户 checkout。"""

        await self._ensure_loaded()
        async with self._lock:
            existing = self._projects.get(project_id)
            if existing is None:
                return None
            changes = {}
            if "name" in payload.model_fields_set:
                if not payload.name or not payload.name.strip():
                    raise ProjectValidationError("项目名称不能为空")
                changes["name"] = payload.name.strip()
            if "target_branch" in payload.model_fields_set:
                branch = (payload.target_branch or "").strip()
                if branch:
                    try:
                        await asyncio.to_thread(_git, Path(existing.root_path), "check-ref-format", "--branch", branch)
                        await asyncio.to_thread(_git, Path(existing.root_path), "show-ref", "--verify", f"refs/heads/{branch}")
                    except GitWorkspaceError as exc:
                        raise ProjectValidationError("目标分支必须是已有的本地分支") from exc
                changes["target_branch"] = branch or None
            updated = existing.model_copy(
                update={
                    **changes,
                    "updated_at": datetime.now(timezone.utc),
                    "revision": existing.revision + 1,
                }
            )
            next_map = {**self._projects, project_id: updated}
            await self._commit_locked(next_map)
            self._projects = next_map
            return updated

    async def delete(self, project_id: str) -> bool:
        """只删索引记录；root_path 指向的磁盘目录永远不被本方法触碰。"""

        await self._ensure_loaded()
        async with self._lock:
            if project_id not in self._projects:
                return False
            next_map = {k: v for k, v in self._projects.items() if k != project_id}
            await self._commit_locked(next_map)
            self._projects = next_map
            return True

    async def adjust_session_count(self, project_id: str, delta: int) -> ProjectRecord | None:
        """SessionStore 在该 Project 下创建/删除/迁移会话时调用，仅用于列表展示。"""

        await self._ensure_loaded()
        async with self._lock:
            existing = self._projects.get(project_id)
            if existing is None:
                return None
            updated = existing.model_copy(
                update={
                    "session_count": max(0, existing.session_count + delta),
                    "updated_at": datetime.now(timezone.utc),
                    "revision": existing.revision + 1,
                }
            )
            next_map = {**self._projects, project_id: updated}
            await self._commit_locked(next_map)
            self._projects = next_map
            return updated

    async def _commit_locked(self, next_map: dict[str, ProjectRecord]) -> None:
        """调用方必须已持有 self._lock；整表原子落盘，带有限次 CAS 重试。

        单 worker 部署下竞争只可能来自同进程协程，已经被 self._lock 串行化，
        这里的重试主要是防御性的（多进程/未来迁移存储实现时仍然安全）。
        """

        if self._storage is None:
            self._revision += 1
            return
        expected = self._revision
        for _ in range(_MAX_CAS_ATTEMPTS):
            payload = {
                "schemaVersion": 1,
                "revision": expected + 1,
                "projects": [
                    record.model_dump(by_alias=True, mode="json") for record in next_map.values()
                ],
            }
            if await self._storage.compare_and_swap_json(
                _INDEX_KEY, expected_revision=expected, payload=payload
            ):
                self._revision = expected + 1
                return
            raw = await self._storage.read_json(_INDEX_KEY)
            expected = int(raw.get("revision") or 0) if isinstance(raw, dict) else 0
        raise RuntimeError("Failed to persist project index after repeated CAS conflicts")
