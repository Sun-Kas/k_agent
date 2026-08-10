"""Access Layer 拥有的 MCP/Skill 目录与运行时载荷装配。

在请求链路中的角色：维护前端选择器用的轻量摘要（catalog JSON），并在
`gateway.run` / Team Runtime 发起执行前，把所选 ID 解析成自包含的
MCP 连接配置 + Skill 正文，随请求发给 Agent Backend。

服务边界：
- 列表数据与正文归属本层（`$K_AGENT_HOME` 下 catalog / skills / mcp 配置）
- 后端从不读 catalog 文件；只消费请求体内的完整 entries
- `selected_runtime` 在接入边界校验「未知/已禁用」ID，失败则抛 CatalogError
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.home import (
    mcp_catalog_path as default_mcp_catalog_path,
    mcp_config_path as default_mcp_config_path,
    skills_catalog_path as default_skills_catalog_path,
    skills_dir as default_skills_dir,
)
from backend.mcp_tool.config import load_scoped_mcp_servers
from backend.skills.frontmatter import parse_bool, parse_markdown_frontmatter, split_frontmatter_list
from backend.storage import write_json_atomic


class CatalogError(ValueError):
    """请求的目录项缺失、禁用或配置不完整时抛出。"""


class RuntimeCatalog:
    """持有轻量列表数据，并将用户选择的 ID 解析为后端可直接消费的运行时条目。"""

    def __init__(
        self,
        *,
        mcp_catalog_path: Path | None = None,
        skill_catalog_path: Path | None = None,
        skill_dir: Path | None = None,
        mcp_config_path: Path | None = None,
    ) -> None:
        """绑定 catalog / skills 目录 / MCP 配置路径（测试可注入覆盖）。"""
        self.mcp_catalog_path = mcp_catalog_path or default_mcp_catalog_path()
        self.skill_catalog_path = skill_catalog_path or default_skills_catalog_path()
        self.skill_dir = skill_dir or default_skills_dir()
        self.mcp_config_path = mcp_config_path or default_mcp_config_path()

    def ensure(self) -> None:
        """升级场景：若 catalog 尚不存在，从现有配置/Skill 目录一次性生成。"""
        if not self.mcp_catalog_path.exists():
            servers, _ = read_mcp_config(self.mcp_config_path)
            self.write_mcp_summaries(
                [
                    {
                        "id": str(server.get("id") or ""),
                        "name": str(server.get("name") or server.get("id") or ""),
                        "description": str(server.get("description") or ""),
                        "enabled": bool(server.get("enabled", True)),
                    }
                    for server in servers
                    if server.get("id")
                ]
            )
        if not self.skill_catalog_path.exists():
            summaries = []
            if self.skill_dir.is_dir():
                for entry in sorted(self.skill_dir.iterdir(), key=lambda item: item.name):
                    skill_file = entry / "SKILL.md"
                    if not entry.is_dir() or not skill_file.is_file():
                        continue
                    frontmatter, content = parse_markdown_frontmatter(
                        skill_file.read_text(encoding="utf-8", errors="replace")
                    )
                    summaries.append(
                        {
                            "id": entry.name,
                            "name": str(frontmatter.get("name") or entry.name),
                            "description": str(
                                frontmatter.get("description")
                                or _first_content_line(content)
                                or entry.name
                            ),
                            "enabled": not parse_bool(
                                frontmatter.get("disable-model-invocation"), False
                            ),
                        }
                    )
            self.write_skill_summaries(summaries)

    def list_payload(self) -> dict[str, Any]:
        """返回工作台选择器的轻量列表，不调用 Agent Backend。"""
        return {
            "mcpServers": self.mcp_summaries(),
            "skills": self.skill_summaries(),
            "sources": {
                "mcp": str(self.mcp_catalog_path),
                "skills": str(self.skill_catalog_path),
            },
        }

    def mcp_summaries(self) -> list[dict[str, Any]]:
        """读取 MCP 目录摘要（id/name/description/enabled）。"""
        return _read_list(self.mcp_catalog_path, "servers")

    def skill_summaries(self) -> list[dict[str, Any]]:
        """读取 Skill 目录摘要（id/name/description/enabled）。"""
        return _read_list(self.skill_catalog_path, "skills")

    def write_mcp_summaries(self, servers: list[dict[str, Any]]) -> None:
        """原子写入 MCP 摘要列表（配置中心保存后调用）。"""
        _write_list(self.mcp_catalog_path, "servers", [_summary(item) for item in servers])

    def write_skill_summaries(self, skills: list[dict[str, Any]]) -> None:
        """原子写入 Skill 摘要列表。"""
        _write_list(self.skill_catalog_path, "skills", [_summary(item) for item in skills])

    def selected_runtime(
        self, mcp_ids: list[str], skill_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """在接入边界解析 ID，返回自包含的 MCP 配置与 Skill 运行时条目。

        摘要只负责选择与启用态；真实连接参数来自 mcp 配置文件，Skill 正文
        来自 content/skills/<id>/SKILL.md。后端不再二次读盘。
        """
        mcp_summaries = self._select(self.mcp_summaries(), mcp_ids, "MCP")
        skill_summaries = self._select(self.skill_summaries(), skill_ids, "Skill")

        scoped = load_scoped_mcp_servers(
            explicit_config_path=str(self.mcp_config_path)
        )
        mcp_configs = {
            server.id: {
                "id": server.id,
                "scope": server.scope.value,
                "type": server.type.value,
                "command": server.command,
                "args": server.args,
                "env": server.env,
                "envPassthrough": server.env_passthrough,
                "cwd": server.cwd,
                "url": server.url,
                "bearerTokenEnv": server.bearer_token_env,
                "headers": server.headers,
                "envHeaders": server.env_headers,
                "enabled": server.enabled,
                "sourcePath": server.source_path,
            }
            for server in scoped.servers
        }
        missing_configs = [item["id"] for item in mcp_summaries if item["id"] not in mcp_configs]
        if missing_configs:
            raise CatalogError(
                "Missing MCP runtime config: " + ", ".join(sorted(missing_configs))
            )
        selected_mcp = [
            {
                **mcp_configs[item["id"]],
                "name": item["name"],
                "description": item["description"],
            }
            for item in mcp_summaries
        ]
        selected_skills = [self._skill_runtime(item) for item in skill_summaries]
        return selected_mcp, selected_skills

    @staticmethod
    def _select(
        entries: list[dict[str, Any]], ids: list[str], label: str
    ) -> list[dict[str, Any]]:
        """按请求 ID 顺序选取已启用条目；未知或禁用则 CatalogError。"""
        if not ids:
            return []
        enabled = {
            str(item.get("id")): item
            for item in entries
            if item.get("id") and item.get("enabled", True)
        }
        missing = sorted(set(ids) - set(enabled))
        if missing:
            raise CatalogError(
                f"Unknown or disabled {label}: " + ", ".join(missing)
            )
        return [enabled[item_id] for item_id in dict.fromkeys(ids)]

    def _skill_runtime(self, summary: dict[str, Any]) -> dict[str, Any]:
        """把摘要扩展为含 instructions / 工具约束等的后端 Skill 载荷。"""
        skill_id = str(summary["id"])
        skill_file = self.skill_dir / skill_id / "SKILL.md"
        if not skill_file.is_file():
            raise CatalogError(f'Missing Skill instructions: "{skill_id}"')
        frontmatter, content = parse_markdown_frontmatter(
            skill_file.read_text(encoding="utf-8", errors="replace")
        )
        return {
            **summary,
            "instructions": content.strip(),
            "filePath": str(skill_file.resolve()),
            "baseDir": str(skill_file.parent.resolve()),
            "allowedTools": split_frontmatter_list(frontmatter.get("allowed-tools")),
            "argumentHint": frontmatter.get("argument-hint"),
            "argumentNames": split_frontmatter_list(frontmatter.get("arguments")),
            "whenToUse": frontmatter.get("when_to_use"),
            "model": (
                frontmatter.get("model")
                if frontmatter.get("model") not in (None, "inherit")
                else None
            ),
            "executionContext": (
                "fork" if frontmatter.get("context") == "fork" else "inline"
            ),
            "hooks": (
                frontmatter.get("hooks")
                if isinstance(frontmatter.get("hooks"), dict)
                else {}
            ),
        }


def read_mcp_config(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """读取 Claude 风格 mcpServers 或旧版 servers 数组，返回 (列表, 格式名)。"""
    if not path.exists():
        return [], None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload.get("mcpServers"), dict):
        return (
            [
                {"id": key, **value}
                for key, value in payload["mcpServers"].items()
                if isinstance(value, dict)
            ],
            "mcpServers",
        )
    return list(payload.get("servers", [])), "servers"


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    item_id = str(item.get("id") or "").strip()
    return {
        "id": item_id,
        "name": str(item.get("name") or item_id),
        "description": str(item.get("description") or ""),
        "enabled": bool(item.get("enabled", True)),
    }


def _read_list(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get(key, [])
    return [_summary(item) for item in values if isinstance(item, dict) and item.get("id")]


def _write_list(path: Path, key: str, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {key: values})


def _first_content_line(content: str) -> str:
    for line in content.splitlines():
        value = line.strip("# ").strip()
        if value:
            return value[:500]
    return ""
