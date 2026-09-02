"""Access Layer 拥有的 MCP/Skill 目录与运行时载荷装配。

在请求链路中的角色：维护前端选择器用的轻量摘要（catalog JSON），并在
`gateway.run` / Team Runtime 发起执行前，把所选 ID 解析成 MCP 连接配置
和 Skill catalog 元数据，随请求发给 Agent Backend。

服务边界：
- Skill catalog 及导入/编辑归属本层（`$K_AGENT_HOME` 下 catalog / skills / mcp 配置）
- run 时只读 catalog/skills.json，不检查或读取 SKILL.md；正文由 Backend 的 Skill 工具按需读取
- `selected_runtime` 在接入边界校验「未知/已禁用」ID，失败则抛 CatalogError
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from access_layer.home import (
    mcp_catalog_path as default_mcp_catalog_path,
    mcp_config_path as default_mcp_config_path,
    skills_catalog_path as default_skills_catalog_path,
    skills_dir as default_skills_dir,
)
from access_layer.mcp_config import load_scoped_mcp_servers
from access_layer.skills.frontmatter import parse_bool, parse_markdown_frontmatter, split_frontmatter_list
from access_layer.storage import write_json_atomic


class CatalogError(ValueError):
    """请求的目录项缺失、禁用或配置不完整时抛出。"""


class RuntimeCatalog:
    """持有轻量列表数据，并将用户选择的 ID 解析为后端可消费的运行时条目。"""

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
                        skill_catalog_row(
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
                                **catalog_fields_from_frontmatter(frontmatter),
                            }
                        )
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
        return _read_list(self.mcp_catalog_path, "servers", _mcp_summary)

    def skill_summaries(self) -> list[dict[str, Any]]:
        """读取 Skill 目录（id/name/description/enabled + frontmatter 元数据）。"""
        return _read_list(self.skill_catalog_path, "skills", skill_catalog_row)

    def write_mcp_summaries(self, servers: list[dict[str, Any]]) -> None:
        """原子写入 MCP 摘要列表（配置中心保存后调用）。"""
        _write_list(self.mcp_catalog_path, "servers", [_mcp_summary(item) for item in servers])

    def write_skill_summaries(self, skills: list[dict[str, Any]]) -> None:
        """原子写入 Skill 目录。不得写入 SKILL.md 正文。"""
        _write_list(self.skill_catalog_path, "skills", [skill_catalog_row(item) for item in skills])

    def selected_runtime(
        self, mcp_ids: list[str], skill_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """在接入边界解析 ID，返回本轮勾选的 MCP 配置与 Skill 元数据。

        Skill 条目完全来自 catalog/skills.json；本轮只按 id 选取整行，不检查
        Skill 包是否存在，也不读取 SKILL.md。Backend 只有在模型真正调用 Skill
        工具后，才按 id 懒加载正文；文件中的 frontmatter 不参与运行时元数据。

        输入示例::

            mcp_ids = ["filesystem"]
            skill_ids = ["skill-creator"]

        输出示例（``(selected_mcp, selected_skills)``）::

            (
              [{
                "id": "filesystem",
                ...
              }],
              [{
                "id": "skill-creator",
                "name": "skill-creator",
                "description": "...",
                "enabled": True,
                "allowedTools": [],
                "argumentHint": None,
                "argumentNames": [],
                "whenToUse": None,
                "model": None,
                "executionContext": "inline",
                "hooks": {},
              }],
            )
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
        # 这里故意直接返回 catalog 行：Access Layer 的 run 路径不得根据 ID
        # 打开 Skill 包，否则正文会在模型尚未决定使用 Skill 前被提前加载。
        return selected_mcp, skill_summaries

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


def catalog_fields_from_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """SKILL.md YAML → catalog/skills.json 的完整受支持字段（不含正文）。"""
    model = frontmatter.get("model")
    context = frontmatter.get("context") or frontmatter.get("executionContext")
    hooks = frontmatter.get("hooks")
    return {
        "allowedTools": split_frontmatter_list(
            frontmatter.get("allowed-tools") or frontmatter.get("allowedTools")
        ),
        "argumentHint": frontmatter.get("argument-hint") or frontmatter.get("argumentHint"),
        "argumentNames": split_frontmatter_list(
            frontmatter.get("arguments")
            if frontmatter.get("arguments") is not None
            else frontmatter.get("argumentNames")
        ),
        "whenToUse": _optional_str(frontmatter.get("when_to_use") or frontmatter.get("whenToUse")),
        "version": _optional_str(frontmatter.get("version")),
        "model": None if model in (None, "inherit") else model,
        "disableModelInvocation": parse_bool(
            frontmatter.get("disable-model-invocation")
            if frontmatter.get("disable-model-invocation") is not None
            else frontmatter.get("disableModelInvocation"),
            False,
        ),
        "userInvocable": parse_bool(
            frontmatter.get("user-invocable")
            if frontmatter.get("user-invocable") is not None
            else frontmatter.get("userInvocable"),
            True,
        ),
        "executionContext": "fork" if context == "fork" else "inline",
        "agent": _optional_str(frontmatter.get("agent")),
        "paths": split_frontmatter_list(frontmatter.get("paths")),
        "hooks": hooks if isinstance(hooks, dict) else {},
    }


def skill_catalog_row(item: dict[str, Any]) -> dict[str, Any]:
    """规范化 catalog 一行；丢弃 instructions 等正文。"""
    item_id = str(item.get("id") or "").strip()
    return {
        "id": item_id,
        "name": str(item.get("name") or item_id),
        "description": str(item.get("description") or ""),
        "enabled": bool(item.get("enabled", True)),
        **catalog_fields_from_frontmatter(item),
    }


def _mcp_summary(item: dict[str, Any]) -> dict[str, Any]:
    item_id = str(item.get("id") or "").strip()
    return {
        "id": item_id,
        "name": str(item.get("name") or item_id),
        "description": str(item.get("description") or ""),
        "enabled": bool(item.get("enabled", True)),
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_raw_list(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get(key, [])
    return [item for item in values if isinstance(item, dict) and item.get("id")]


def _read_list(path: Path, key: str, normalize) -> list[dict[str, Any]]:
    return [normalize(item) for item in _read_raw_list(path, key)]


def _write_list(path: Path, key: str, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, {key: values})


def _first_content_line(content: str) -> str:
    for line in content.splitlines():
        value = line.strip("# ").strip()
        if value:
            return value[:500]
    return ""
