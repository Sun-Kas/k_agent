"""Project 的公开请求/持久化模型。

Project 本质上只是「一个稳定文件夹 + 一份归属关系」：不带 description、
default_mcp_server_ids、permission_mode 等重字段——那些仍然是 Session 级配置。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreateInput(BaseModel):
    """创建 Project：只需要一个绝对路径；名称可留空取目录名。"""

    model_config = ConfigDict(populate_by_name=True)

    # 可选；留空时取 Path(root_path).name 作为默认值（仅创建时计算一次）。
    name: str | None = Field(default=None, max_length=120)
    # 必填、必须是绝对路径；相对路径/空字符串在 ProjectStore 里直接拒绝。
    root_path: str = Field(alias="rootPath", min_length=1, max_length=4096)


class ProjectRecord(BaseModel):
    """落盘的 Project 索引记录（state/projects/index.json 里的一条）。"""

    model_config = ConfigDict(populate_by_name=True)

    # sha256(resolve(root_path).as_posix())[:16]；创建时算一次，此后不变。
    # 同一 root_path 创建两次会天然得到同一个 id（去重），不会生成重复记录。
    id: str
    # 创建时默认等于 Path(root_path).name；此后独立存储、可通过 PATCH 单独修改。
    # 改名不会触碰磁盘目录；磁盘目录改名也不会回写这里。
    name: str
    # 绝对路径，入库前已 resolve；创建后不可变（改路径语义上等于换一个 Project）。
    root_path: str = Field(alias="rootPath")
    is_git_repository: bool = Field(default=False, alias="isGitRepository")
    repository_root: str | None = Field(default=None, alias="repositoryRoot")
    git_common_dir: str | None = Field(default=None, alias="gitCommonDir")
    target_branch: str | None = Field(default=None, alias="targetBranch")
    # 归属该 Project 的会话数量；由 SessionStore 在创建/删除/迁移会话时维护，
    # 仅用于列表展示，不是任何校验的真相源。
    session_count: int = Field(default=0, alias="sessionCount")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    # 索引表级别的乐观锁版本号（配合 FileStorage.compare_and_swap_json）。
    revision: int = 0


class ProjectUpdateInput(BaseModel):
    """项目名称和成果汇总分支；路径保持稳定。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    target_branch: str | None = Field(default=None, alias="targetBranch", max_length=255)


class ProjectSummary(BaseModel):
    """列表/侧栏展示用的轻量摘要，字段与 ProjectRecord 对外部分一致。"""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    root_path: str = Field(alias="rootPath")
    is_git_repository: bool = Field(default=False, alias="isGitRepository")
    repository_root: str | None = Field(default=None, alias="repositoryRoot")
    git_common_dir: str | None = Field(default=None, alias="gitCommonDir")
    target_branch: str | None = Field(default=None, alias="targetBranch")
    session_count: int = Field(default=0, alias="sessionCount")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class SessionStartingState(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["default", "branch", "working-tree"] = "default"
    branch_name: str | None = Field(default=None, alias="branchName", min_length=1, max_length=255)
    on_missing: Literal["error", "create-branch"] = Field(default="error", alias="onMissing")


class ProjectSessionCreateInput(BaseModel):
    """auto 默认共享项目目录；worktree 仅由用户显式选择。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    environment: Literal["auto", "local", "worktree"] = "auto"
    start_ref: str | None = Field(default=None, alias="startRef", max_length=512)
    starting_state: SessionStartingState = Field(default_factory=SessionStartingState, alias="startingState")


class RepositoryInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    is_git_repository: bool = Field(alias="isGitRepository")
    repository_root: str | None = Field(default=None, alias="repositoryRoot")
    git_common_dir: str | None = Field(default=None, alias="gitCommonDir")
    head_oid: str | None = Field(default=None, alias="headOid")
    branch_ref: str | None = Field(default=None, alias="branchRef")
    detached: bool = False
