from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi import HTTPException

from access_layer.catalog import RuntimeCatalog
from access_layer.marketplace.install_mcp import map_install
from access_layer.marketplace.match import match_installed
from access_layer.marketplace.mcp_registry import RegistryError
from access_layer.marketplace.service import MarketplaceService
from access_layer.marketplace.skillhub import SkillHubClient, SkillHubError, _unwrap, normalize_skill_detail
from access_layer.skills.archive import validate_and_install_skill_zip
from access_layer.storage import write_json_atomic


def _catalog(root: Path) -> RuntimeCatalog:
    mcp_catalog = root / "catalog-mcp.json"
    skill_catalog = root / "catalog-skills.json"
    mcp_config = root / "mcp.json"
    write_json_atomic(mcp_catalog, {"servers": []})
    write_json_atomic(skill_catalog, {"skills": []})
    write_json_atomic(mcp_config, {"mcpServers": {}})
    return RuntimeCatalog(
        mcp_catalog_path=mcp_catalog,
        skill_catalog_path=skill_catalog,
        skill_dir=root / "skills",
        mcp_config_path=mcp_config,
    )


NPM_SERVER = {
    "name": "io.github.user/brave-search",
    "title": "Brave Search",
    "description": "Search the web",
    "version": "1.0.2",
    "packages": [
        {
            "registryType": "npm",
            "identifier": "@modelcontextprotocol/server-brave-search",
            "version": "1.0.2",
            "environmentVariables": [
                {
                    "name": "BRAVE_API_KEY",
                    "description": "Brave Search API Key",
                    "isRequired": True,
                    "isSecret": True,
                }
            ],
        }
    ],
}

HTTP_SERVER = {
    "name": "ai.smithery/github",
    "title": "GitHub",
    "description": "GitHub API",
    "version": "1.0.0",
    "remotes": [
        {
            "type": "streamable-http",
            "url": "https://server.smithery.ai/github/mcp",
            "headers": [
                {
                    "name": "Authorization",
                    "description": "Bearer token",
                    "isRequired": True,
                    "isSecret": True,
                    "value": "Bearer {smithery_api_key}",
                }
            ],
        }
    ],
}

OCI_SERVER = {
    "name": "io.example/boxed",
    "title": "Boxed",
    "description": "oci only",
    "packages": [{"registryType": "oci", "identifier": "example/boxed"}],
}


class FakeRegistry:
    def __init__(self, servers: list[dict], *, fail_list: bool = False) -> None:
        self.servers = servers
        self.pages: list[int] = []
        self.fail_list = fail_list

    async def list_servers(self, *, search="", page=1, page_size=20):
        self.pages.append(page)
        if self.fail_list:
            raise RegistryError("down", status_code=503)
        items = [
            {
                "id": server.get("id") or server["name"],
                "name": server.get("title") or server.get("name"),
                "description": server.get("description") or "",
            }
            for server in self.servers
        ]
        return {"items": items, "total": len(items), "page": page, "pageSize": page_size}

    async def get_latest(self, source_id: str):
        for server in self.servers:
            if server.get("name") == source_id or server.get("id") == source_id:
                return {"server": server} if "packages" in server or "remotes" in server else server
        raise RegistryError("missing", status_code=404)


class FakeSkillHub:
    def __init__(self) -> None:
        self.api_key = "secret-key-should-not-leak"
        self.downloaded = 0

    async def list_skills(self, **kwargs):
        return {
            "total": 1,
            "page": 1,
            "skills": [{"slug": "find-skill-skillhub", "name": "Find Skill", "description": "搜索技能", "downloads": 12}],
        }

    async def top(self):
        return await self.list_skills()

    async def categories(self):
        return [{"id": "dev-programming", "name": "开发"}]

    async def skill_detail(self, slug: str):
        return {
            "skill": {
                "slug": slug,
                "displayName": "Find Skill",
                "summary_zh": "搜索技能",
                "category": "ai-agent",
                "tags": {"latest": "1.0.0"},
                "stats": {"downloads": 12, "installs": 3},
            },
            "latestVersion": {"version": "1.0.0", "changelog": "初始版本"},
            "owner": {"handle": "alice", "displayName": "Alice"},
        }

    async def skill_markdown(self, slug: str) -> str:
        return "---\nname: Find Skill\ndescription: 搜索技能\n---\n\n# Find Skill\n\n用于搜索技能。\n"

    async def evaluation(self, slug: str):
        return {"score": 90, "grade": "A", "secretPrompt": "do not leak"}

    async def versions(self, slug: str):
        return {"versions": [{"version": "1.0.0", "createdAt": 1}]}

    async def download_zip(self, slug: str) -> bytes:
        self.downloaded += 1
        raise SkillHubError("boom", code="network")


class MappingTests(unittest.TestCase):
    def test_npm_maps_to_npx_and_required_secret(self) -> None:
        mapped = map_install(NPM_SERVER)
        self.assertEqual(mapped["transport"], "stdio")
        self.assertEqual(mapped["command"], "npx")
        self.assertEqual(mapped["args"][:2], ["-y", "@modelcontextprotocol/server-brave-search@1.0.2"])
        self.assertEqual(mapped["missingEnvKeys"], ["BRAVE_API_KEY"])
        self.assertIn("BRAVE_API_KEY", mapped["secretKeys"])

    def test_http_remote_maps_headers(self) -> None:
        mapped = map_install(HTTP_SERVER)
        self.assertEqual(mapped["transport"], "http")
        self.assertEqual(mapped["draft"]["url"], "https://server.smithery.ai/github/mcp")
        self.assertEqual(mapped["missingEnvKeys"], ["Authorization"])

    def test_oci_is_blocked(self) -> None:
        self.assertEqual(map_install(OCI_SERVER)["blockedReason"], "unsupported_package")

    def test_modelscope_npx_and_required_env(self) -> None:
        mapped = map_install(
            {
                "id": "@amap/amap-maps",
                "chinese_name": "高德地图",
                "description": "地图服务",
                "server_config": [
                    {
                        "mcpServers": {
                            "amap-maps": {
                                "args": ["-y", "@amap/amap-maps-mcp-server"],
                                "command": "npx",
                                "env": {"AMAP_MAPS_API_KEY": ""},
                            }
                        }
                    }
                ],
                "env_schema": {
                    "type": "object",
                    "required": ["AMAP_MAPS_API_KEY"],
                    "properties": {"AMAP_MAPS_API_KEY": {"description": "高德 key", "type": "string"}},
                },
            }
        )
        self.assertEqual(mapped["command"], "npx")
        self.assertEqual(mapped["missingEnvKeys"], ["AMAP_MAPS_API_KEY"])
        self.assertEqual(mapped["title"], "高德地图")


class MatchTests(unittest.TestCase):
    def test_strong_source_id_wins(self) -> None:
        rows = [
            {"id": "other", "marketplace": {"source": "mcp-registry", "sourceId": "io.github.user/brave-search"}},
            {"id": "brave-search"},
        ]
        matched = match_installed(rows, source="mcp-registry", source_id="io.github.user/brave-search")
        self.assertEqual(matched["id"], "other")

    def test_weak_match_does_not_install(self) -> None:
        rows = [{"id": "brave-search"}]
        matched = match_installed(rows, source="mcp-registry", source_id="io.github.user/brave-search")
        self.assertEqual(matched["id"], "brave-search")


class SkillHubParseTests(unittest.TestCase):
    def test_list_envelope_unwraps_data(self) -> None:
        payload = _unwrap({"code": 0, "message": "success", "data": {"skills": []}}, envelope=True)
        self.assertEqual(payload, {"skills": []})

    def test_v1_payload_stays_object(self) -> None:
        payload = _unwrap({"slug": "find-skill-skillhub"}, envelope=False)
        self.assertEqual(payload["slug"], "find-skill-skillhub")

    def test_headers_omit_empty_key(self) -> None:
        client = SkillHubClient(base_url="https://api.skillhub.cn", api_key="", timeout_seconds=1)
        self.assertNotIn("X-API-Key", client.headers())

    def test_nested_detail_flattens(self) -> None:
        payload = normalize_skill_detail(
            {
                "skill": {
                    "slug": "find-skill-skillhub",
                    "displayName": "find skill",
                    "summary_zh": "中文简介",
                    "category": "ai-agent",
                    "tags": {"latest": "1.0.2"},
                    "stats": {"downloads": 10},
                },
                "latestVersion": {"version": "1.0.2", "changelog": "bugfix"},
                "owner": {"handle": "alice", "displayName": "Alice"},
            }
        )
        self.assertEqual(payload["name"], "find skill")
        self.assertEqual(payload["description"], "中文简介")
        self.assertEqual(payload["version"], "1.0.2")
        self.assertEqual(payload["ownerName"], "Alice")
        self.assertEqual(payload["changelog"], "bugfix")


class MarketplaceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_passes_cursor_and_does_not_write_catalog(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = _catalog(root)
            registry = FakeRegistry([NPM_SERVER])
            service = MarketplaceService(catalog, mcp_config_path=root / "mcp.json", mcp_registry=registry, skillhub=FakeSkillHub())
            listed = await service.list_mcp(page=2, page_size=20)
            self.assertEqual(registry.pages, [2])
            self.assertEqual(listed["page"]["page"], 2)
            self.assertEqual(listed["page"]["total"], 1)
            self.assertEqual(listed["items"][0]["sourceId"], "io.github.user/brave-search")
            self.assertEqual(listed["items"][0]["source"], "modelscope")
            self.assertNotIn("SKILLHUB", json.dumps(listed))
            self.assertEqual(catalog.list_payload()["mcpServers"], [])

    async def test_unavailable_registry_returns_200_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = MarketplaceService(
                _catalog(root),
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([], fail_list=True),
                skillhub=FakeSkillHub(),
            )
            listed = await service.list_mcp()
            self.assertEqual(listed["sourceStatus"], "unavailable")
            self.assertEqual(listed["items"], [])

    async def test_preview_skill_includes_detail_and_markdown(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = MarketplaceService(
                _catalog(root),
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([]),
                skillhub=FakeSkillHub(),
            )
            preview = await service.preview_skill("find-skill-skillhub")
            self.assertEqual(preview["title"], "Find Skill")
            self.assertEqual(preview["ownerName"], "Alice")
            self.assertIn("# Find Skill", preview["body"] or "")
            self.assertEqual(preview["conflict"]["exists"], False)

    async def test_missing_env_does_not_write_mcp_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = _catalog(root)
            service = MarketplaceService(
                catalog,
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([NPM_SERVER]),
                skillhub=FakeSkillHub(),
            )
            with self.assertRaises(HTTPException) as raised:
                await service.install_mcp("io.github.user/brave-search", env={})
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(json.loads((root / "mcp.json").read_text())["mcpServers"], {})
            self.assertEqual(catalog.mcp_summaries(), [])

    async def test_mcp_install_then_selected_runtime_strips_marketplace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = _catalog(root)
            service = MarketplaceService(
                catalog,
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([NPM_SERVER]),
                skillhub=FakeSkillHub(),
            )
            result = await service.install_mcp(
                "io.github.user/brave-search",
                env={"BRAVE_API_KEY": "secret"},
            )
            self.assertEqual(result["localId"], "brave-search")
            self.assertEqual(catalog.mcp_summaries()[0]["marketplace"]["sourceId"], "io.github.user/brave-search")
            mcp, _ = catalog.selected_runtime(["brave-search"], [])
            self.assertNotIn("marketplace", mcp[0])
            self.assertEqual(mcp[0]["env"]["BRAVE_API_KEY"], "secret")

    async def test_skill_list_does_not_leak_key(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = MarketplaceService(
                _catalog(root),
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([]),
                skillhub=FakeSkillHub(),
            )
            listed = await service.list_skills()
            blob = json.dumps(listed)
            self.assertNotIn("secret-key-should-not-leak", blob)
            self.assertNotIn("X-API-Key", blob)

    async def test_skill_bad_zip_does_not_leave_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = _catalog(root)
            service = MarketplaceService(
                catalog,
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([]),
                skillhub=FakeSkillHub(),
            )
            with self.assertRaises(HTTPException):
                await service.install_skill("find-skill-skillhub")
            self.assertFalse((catalog.skill_dir / "find-skill").exists())
            self.assertEqual(catalog.skill_summaries(), [])

    async def test_existing_skill_id_is_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = _catalog(root)
            skill_dir = catalog.skill_dir / "find-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: Find Skill\ndescription: x\n---\n\nBody\n", encoding="utf-8")
            catalog.write_skill_summaries(
                [
                    {
                        "id": "find-skill",
                        "name": "Find Skill",
                        "description": "x",
                        "enabled": True,
                        "marketplace": {"source": "skillhub", "sourceId": "find-skill-skillhub"},
                    }
                ]
            )
            service = MarketplaceService(
                catalog,
                mcp_config_path=root / "mcp.json",
                mcp_registry=FakeRegistry([]),
                skillhub=FakeSkillHub(),
            )
            preview = await service.preview_skill("find-skill-skillhub")
            self.assertTrue(preview["conflict"]["exists"])

    async def test_selected_runtime_strips_skill_marketplace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = _catalog(root)
            catalog.write_skill_summaries(
                [
                    {
                        "id": "writer",
                        "name": "写作",
                        "description": "帮助写作",
                        "enabled": True,
                        "marketplace": {"source": "skillhub", "sourceId": "writer-slug", "version": "1"},
                    }
                ]
            )
            _, skills = catalog.selected_runtime([], ["writer"])
            self.assertEqual(skills[0]["id"], "writer")
            self.assertNotIn("marketplace", skills[0])


class SkillZipHostTests(unittest.TestCase):
    def test_validate_rejects_non_zip_without_creating_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(HTTPException):
                validate_and_install_skill_zip(b"not-a-zip", "skill.zip", skills_root=root)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
