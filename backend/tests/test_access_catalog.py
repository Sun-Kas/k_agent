from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from access_layer.catalog import CatalogError, RuntimeCatalog
from access_layer.main import _merge_mcp_runtime_status
from access_layer.schemas import McpServerInput
from pydantic import ValidationError


class AccessCatalogTests(unittest.TestCase):
    def test_bearer_token_field_accepts_env_name_not_token_value(self) -> None:
        valid = McpServerInput(
            id="calendar",
            type="http",
            url="https://example.test/mcp",
            bearerTokenEnv="MCP_BEARER_TOKEN",
        )
        self.assertEqual(valid.bearer_token_env, "MCP_BEARER_TOKEN")

        with self.assertRaises(ValidationError):
            McpServerInput(
                id="calendar",
                type="http",
                url="https://example.test/mcp",
                bearerTokenEnv="token-with-dashes",
            )

    def test_stdio_hidden_remote_fields_accept_blank_form_values(self) -> None:
        server = McpServerInput(
            id="flight-price-compare-mcp",
            type="stdio",
            command="uvx",
            args=["flight-price-compare-mcp==3.3.3"],
            cwd="",
            url="",
            bearerTokenEnv="",
        )

        self.assertIsNone(server.cwd)
        self.assertIsNone(server.url)
        self.assertIsNone(server.bearer_token_env)

    def test_runtime_status_exposes_connection_feedback(self) -> None:
        servers = [
            {
                "id": "calendar",
                "name": "日历",
                "enabled": True,
                "type": "http",
                "toolCount": 0,
                "resourceCount": 0,
            }
        ]

        merged = _merge_mcp_runtime_status(
            servers,
            [
                {
                    "id": "calendar",
                    "scope": "local",
                    "type": "http",
                    "status": "failed",
                    "tool_count": 0,
                    "resource_count": 0,
                    "error": "401 Unauthorized",
                }
            ],
        )

        self.assertFalse(merged[0]["connected"])
        self.assertEqual(merged[0]["status"], "failed")
        self.assertEqual(merged[0]["error"], "401 Unauthorized")

    def test_list_payload_reads_only_summary_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mcp_path = root / "mcp.json"
            skill_path = root / "skill.json"
            mcp_path.write_text(
                json.dumps(
                    {
                        "servers": [
                            {
                                "id": "calendar",
                                "name": "日历",
                                "description": "管理日程",
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            skill_path.write_text(
                json.dumps(
                    {
                        "skills": [
                            {
                                "id": "writer",
                                "name": "写作",
                                "description": "帮助写作",
                                "enabled": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = RuntimeCatalog(
                mcp_catalog_path=mcp_path,
                skill_catalog_path=skill_path,
                skill_dir=root / "missing-skills",
                mcp_config_path=root / "missing-mcp.json",
            )

            payload = catalog.list_payload()

            self.assertEqual(payload["mcpServers"][0]["description"], "管理日程")
            self.assertEqual(payload["skills"][0]["description"], "帮助写作")

    def test_selected_skill_is_resolved_by_access_layer(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skill"
            (skill_dir / "writer").mkdir(parents=True)
            (skill_dir / "writer" / "SKILL.md").write_text(
                "---\nname: 写作\ndescription: file description\narguments: topic\n---\n\nWrite ${topic}.",
                encoding="utf-8",
            )
            mcp_path = root / "mcp.json"
            skill_path = root / "skill.json"
            mcp_path.write_text('{"servers":[]}', encoding="utf-8")
            skill_path.write_text(
                '{"skills":[{"id":"writer","name":"写作","description":"帮助写作","enabled":true}]}',
                encoding="utf-8",
            )
            catalog = RuntimeCatalog(
                mcp_catalog_path=mcp_path,
                skill_catalog_path=skill_path,
                skill_dir=skill_dir,
                mcp_config_path=root / "runtime-mcp.json",
            )

            _, skills = catalog.selected_runtime([], ["writer"])

            self.assertEqual(skills[0]["description"], "帮助写作")
            self.assertEqual(skills[0]["instructions"], "Write ${topic}.")
            self.assertEqual(skills[0]["argumentNames"], ["topic"])

    def test_disabled_selection_is_rejected_at_access_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mcp_path = root / "mcp.json"
            skill_path = root / "skill.json"
            mcp_path.write_text('{"servers":[]}', encoding="utf-8")
            skill_path.write_text(
                '{"skills":[{"id":"off","name":"off","description":"","enabled":false}]}',
                encoding="utf-8",
            )
            catalog = RuntimeCatalog(
                mcp_catalog_path=mcp_path,
                skill_catalog_path=skill_path,
                skill_dir=root / "skill",
                mcp_config_path=root / "runtime-mcp.json",
            )

            with self.assertRaises(CatalogError):
                catalog.selected_runtime([], ["off"])


if __name__ == "__main__":
    unittest.main()
