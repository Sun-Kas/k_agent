from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from backend.config import Settings
from backend.sandbox import (
    SandboxUnavailable,
    build_child_env,
    build_settings_payload,
    detect_support,
    install_sandbox_runtime,
    notice_from_tool_result,
    plan_bash_invocation,
    reset_sandbox_detection,
    sandbox_runtime_status,
)
from backend.tools import cc_like


def _settings(**overrides: object) -> Settings:
    values = {
        "bash_sandbox_mode": "auto",
        "bash_sandbox_command": "srt",
        "bash_sandbox_allowed_domains": [],
        "bash_sandbox_weaker_network_isolation": True,
        "bash_sandbox_write_paths": [],
        "bash_sandbox_deny_read": [],
    }
    values.update(overrides)
    return Settings.model_validate(values)


class SandboxDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_sandbox_detection()

    def tearDown(self) -> None:
        reset_sandbox_detection()

    def test_windows_is_explicitly_unsupported(self) -> None:
        with patch("backend.sandbox.detect.sys.platform", "win32"):
            support = detect_support("srt")
        self.assertFalse(support.available)
        self.assertIn("WSL2", support.reason)

    def test_missing_srt_is_reported(self) -> None:
        with (
            patch("backend.sandbox.detect.sys.platform", "darwin"),
            patch("backend.sandbox.detect.shutil.which", return_value=None),
        ):
            support = detect_support("srt")
        self.assertFalse(support.available)
        self.assertIn("not found on PATH", support.reason)

    def test_linux_requires_bubblewrap(self) -> None:
        def which(name: str) -> str | None:
            return "/usr/bin/srt" if name == "srt" else None

        with (
            patch("backend.sandbox.detect.sys.platform", "linux"),
            patch("backend.sandbox.detect.shutil.which", side_effect=which),
        ):
            support = detect_support("srt")
        self.assertFalse(support.available)
        self.assertIn("bwrap", support.reason)


class SandboxPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_sandbox_detection()

    def tearDown(self) -> None:
        reset_sandbox_detection()

    def test_mode_off_skips_sandbox(self) -> None:
        invocation = plan_bash_invocation(
            "echo hi",
            workspace_root=Path("/tmp/ws"),
            settings=_settings(bash_sandbox_mode="off"),
        )
        self.assertIsNone(invocation.argv)
        self.assertFalse(invocation.sandboxed)

    def test_required_mode_raises_when_unavailable(self) -> None:
        with patch(
            "backend.sandbox.plan.detect_support",
            return_value=type(
                "S", (), {"available": False, "reason": "missing srt"}
            )(),
        ):
            with self.assertRaises(SandboxUnavailable):
                plan_bash_invocation(
                    "echo hi",
                    workspace_root=Path("/tmp/ws"),
                    settings=_settings(bash_sandbox_mode="required"),
                )

    def test_auto_mode_degrades_when_unavailable(self) -> None:
        with patch(
            "backend.sandbox.plan.detect_support",
            return_value=type(
                "S", (), {"available": False, "reason": "missing srt"}
            )(),
        ):
            invocation = plan_bash_invocation(
                "echo hi",
                workspace_root=Path("/tmp/ws"),
                settings=_settings(bash_sandbox_mode="auto"),
            )
        self.assertIsNone(invocation.argv)
        self.assertFalse(invocation.sandboxed)
        self.assertEqual(invocation.reason, "missing srt")

    def test_available_backend_builds_srt_argv(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            with (
                patch(
                    "backend.sandbox.plan.detect_support",
                    return_value=type("S", (), {"available": True, "reason": "ok"})(),
                ),
                patch(
                    "backend.sandbox.plan._shell_path",
                    return_value="/bin/bash",
                ),
            ):
                invocation = plan_bash_invocation(
                    "ls -la",
                    workspace_root=workspace,
                    settings=_settings(bash_sandbox_mode="auto"),
                )
            self.assertTrue(invocation.sandboxed)
            assert invocation.argv is not None
            self.assertEqual(invocation.argv[0], "srt")
            self.assertEqual(invocation.argv[1], "--settings")
            settings_path = Path(invocation.argv[2])
            self.assertTrue(settings_path.exists())
            self.assertEqual(invocation.argv[3:], ["/bin/bash", "-c", "ls -la"])
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertIn(str(workspace), payload["filesystem"]["allowWrite"])
            self.assertIn(str(workspace / ".env"), payload["filesystem"]["denyRead"])
            self.assertIn(str(workspace / ".env"), payload["filesystem"]["denyWrite"])


class SandboxSettingsTests(unittest.TestCase):
    def test_default_settings_use_concrete_srt_domains(self) -> None:
        from backend.sandbox.constants import DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS

        settings = Settings(_env_file=None)
        self.assertTrue(settings.network_access_default)
        self.assertEqual(
            settings.bash_sandbox_allowed_domains,
            list(DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS),
        )
        self.assertNotIn("*", settings.bash_sandbox_allowed_domains)

    def test_settings_payload_strips_bare_star_domain(self) -> None:
        payload = build_settings_payload(
            workspace_root=Path("/tmp/ws"),
            settings=_settings(bash_sandbox_allowed_domains=["*", "example.com", "*.com"]),
            network_access=True,
        )
        # Bare "*" / "*.com" are invalid for srt; keep sandbox with valid hosts.
        self.assertEqual(payload["network"]["allowedDomains"], ["example.com"])
        self.assertTrue(payload.get("enableWeakerNetworkIsolation"))

    def test_settings_payload_can_disable_weaker_network_isolation(self) -> None:
        payload = build_settings_payload(
            workspace_root=Path("/tmp/ws"),
            settings=_settings(bash_sandbox_weaker_network_isolation=False),
            network_access=True,
        )
        self.assertNotIn("enableWeakerNetworkIsolation", payload)

    def test_default_allowlist_includes_agently_mail_hosts(self) -> None:
        from backend.sandbox.constants import DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS

        self.assertIn("api.agent.qq.com", DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS)
        self.assertIn("*.agent.qq.com", DEFAULT_BASH_SANDBOX_ALLOWED_DOMAINS)

    def test_plan_keeps_sandbox_when_star_domain_configured(self) -> None:
        from unittest.mock import patch

        from backend.sandbox.detect import SandboxSupport
        from backend.sandbox.plan import plan_bash_invocation

        with patch(
            "backend.sandbox.plan.detect_support",
            return_value=SandboxSupport(available=True, reason="ok"),
        ):
            planned = plan_bash_invocation(
                "echo hi",
                workspace_root=Path("/tmp/ws"),
                settings=_settings(
                    bash_sandbox_mode="auto",
                    bash_sandbox_allowed_domains=["*"],
                ),
                network_access=True,
            )
        self.assertTrue(planned.sandboxed)
        self.assertIsNotNone(planned.argv)
        self.assertEqual(planned.argv[0], "srt")

    def test_settings_deny_credential_stores_and_project_env(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            payload = build_settings_payload(
                workspace_root=workspace,
                settings=_settings(
                    bash_sandbox_allowed_domains=["example.com"],
                    bash_sandbox_write_paths=["~/extra"],
                    bash_sandbox_deny_read=["~/secrets"],
                ),
            )
        deny_read = payload["filesystem"]["denyRead"]
        allow_write = payload["filesystem"]["allowWrite"]
        self.assertTrue(any(path.endswith(".ssh") for path in deny_read))
        self.assertIn(str(Path("~/secrets").expanduser()), deny_read)
        self.assertIn(str(workspace / ".env"), deny_read)
        self.assertIn(str(Path("~/extra").expanduser()), allow_write)
        self.assertEqual(payload["network"]["allowedDomains"], ["example.com"])

    def test_run_override_denies_network_without_disabling_filesystem_sandbox(self) -> None:
        payload = build_settings_payload(
            workspace_root=Path("/tmp/ws"),
            settings=_settings(bash_sandbox_allowed_domains=["*"]),
            network_access=False,
        )
        self.assertEqual(payload["network"]["allowedDomains"], [])
        self.assertIn("/tmp/ws", payload["filesystem"]["allowWrite"])


class ChildEnvTests(unittest.TestCase):
    def test_secrets_are_stripped_from_child_env(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/Users/me",
            "OPENAI_API_KEY": "sk-secret",
            "LANGFUSE_SECRET_KEY": "lf-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "LC_TIME": "en_US.UTF-8",
            "CUSTOM_TOKEN": "nope",
        }
        child = build_child_env(parent)
        self.assertEqual(child["PATH"], "/usr/bin")
        self.assertEqual(child["HOME"], "/Users/me")
        self.assertEqual(child["LC_TIME"], "en_US.UTF-8")
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("LANGFUSE_SECRET_KEY", child)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", child)
        self.assertNotIn("CUSTOM_TOKEN", child)

    def test_extra_allow_list_is_honored(self) -> None:
        child = build_child_env(
            {"PATH": "/bin", "MY_TOOL_HOME": "/opt/tool"},
            extra_allow=("MY_TOOL_HOME",),
        )
        self.assertEqual(child["MY_TOOL_HOME"], "/opt/tool")

    def test_tool_env_overrides_are_merged(self) -> None:
        from backend.sandbox.env import reset_tool_env_overrides, set_tool_env_overrides

        token = set_tool_env_overrides({
            "K_AGENT_SHARED_RUNTIME": "/tmp/runtime",
            "PATH": "/tmp/runtime/node/bin:/bin",
            "NPM_CONFIG_CACHE": "/tmp/runtime/npm-cache",
        })
        try:
            child = build_child_env({"PATH": "/usr/bin", "HOME": "/Users/me"})
            self.assertEqual(child["K_AGENT_SHARED_RUNTIME"], "/tmp/runtime")
            self.assertEqual(child["NPM_CONFIG_CACHE"], "/tmp/runtime/npm-cache")
            self.assertEqual(child["PATH"], "/tmp/runtime/node/bin:/bin")
            self.assertEqual(child["HOME"], "/Users/me")
        finally:
            reset_tool_env_overrides(token)


class BashToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cc_bash_uses_scrubbed_env_and_reports_sandbox_state(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            settings = _settings(bash_sandbox_mode="off")
            with (
                patch.object(cc_like, "_workspace_root", AsyncMock(return_value=workspace)),
                patch.object(cc_like, "get_or_init_settings", AsyncMock(return_value=settings)),
                patch.object(cc_like, "_tool_limits", AsyncMock(return_value=(5.0, 10_000))),
                patch.dict(
                    os.environ,
                    {"OPENAI_API_KEY": "sk-should-not-leak", "PATH": os.environ.get("PATH", "/bin")},
                    clear=False,
                ),
            ):
                raw = await cc_like.cc_bash({"command": "printenv OPENAI_API_KEY || true"})
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["sandboxed"])
        self.assertEqual(payload["sandboxReason"], "sandbox disabled by configuration")
        self.assertEqual((payload.get("stdout") or "").strip(), "")

    async def test_required_mode_surfaces_unavailable_error(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            settings = _settings(bash_sandbox_mode="required", bash_sandbox_command="srt-missing")
            reset_sandbox_detection()
            with (
                patch.object(cc_like, "_workspace_root", AsyncMock(return_value=workspace)),
                patch.object(cc_like, "get_or_init_settings", AsyncMock(return_value=settings)),
                patch.object(cc_like, "_tool_limits", AsyncMock(return_value=(5.0, 10_000))),
                patch("backend.sandbox.detect.shutil.which", return_value=None),
                patch("backend.sandbox.detect.sys.platform", "darwin"),
            ):
                raw = await cc_like.cc_bash({"command": "echo hi"})
            reset_sandbox_detection()
        payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        self.assertIn("sandbox unavailable", payload["error"])
        self.assertFalse(payload["sandboxed"])
        self.assertIn("InstallSandbox", payload["agentInstallHint"])
        self.assertIn("npm install -g", payload["manualInstallCommand"])
        self.assertIn("流程", payload["userMessage"])

    async def test_mode_off_does_not_attach_install_guidance(self) -> None:
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            settings = _settings(bash_sandbox_mode="off")
            with (
                patch.object(cc_like, "_workspace_root", AsyncMock(return_value=workspace)),
                patch.object(cc_like, "get_or_init_settings", AsyncMock(return_value=settings)),
                patch.object(cc_like, "_tool_limits", AsyncMock(return_value=(5.0, 10_000))),
            ):
                raw = await cc_like.cc_bash({"command": "echo hi"})
        payload = json.loads(raw)
        self.assertNotIn("installGuidance", payload)


class InstallGuidanceTests(unittest.TestCase):
    def test_notice_is_emitted_for_unsandboxed_bash(self) -> None:
        message = notice_from_tool_result(
            "Bash",
            json.dumps(
                {
                    "ok": True,
                    "sandboxed": False,
                    "sandboxReason": "'srt' not found on PATH",
                }
            ),
            settings=_settings(bash_sandbox_mode="auto"),
        )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("InstallSandbox", message)
        self.assertIn("npm install -g", message)

    def test_notice_skipped_when_sandbox_disabled(self) -> None:
        message = notice_from_tool_result(
            "Bash",
            json.dumps(
                {
                    "ok": True,
                    "sandboxed": False,
                    "sandboxReason": "sandbox disabled by configuration",
                }
            ),
            settings=_settings(bash_sandbox_mode="off"),
        )
        self.assertIsNone(message)

    def test_health_status_marks_missing_backend(self) -> None:
        reset_sandbox_detection()
        with (
            patch("backend.sandbox.detect.sys.platform", "darwin"),
            patch("backend.sandbox.detect.shutil.which", return_value=None),
        ):
            status = sandbox_runtime_status(
                _settings(bash_sandbox_mode="auto", bash_sandbox_command="srt")
            )
        reset_sandbox_detection()
        self.assertFalse(status["available"])
        self.assertTrue(status["needsInstall"])
        self.assertEqual(status["agentInstallTool"], "InstallSandbox")
        self.assertIn("InstallSandbox", status["userSummary"])

    def test_windows_health_does_not_offer_agent_install(self) -> None:
        reset_sandbox_detection()
        with patch("backend.sandbox.detect.sys.platform", "win32"):
            status = sandbox_runtime_status(_settings(bash_sandbox_mode="auto"))
        reset_sandbox_detection()
        self.assertFalse(status["available"])
        self.assertFalse(status["needsInstall"])
        self.assertIsNone(status["agentInstallTool"])
        self.assertIn("WSL2", status["userSummary"])


class InstallSandboxToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_without_confirmation(self) -> None:
        result = await install_sandbox_runtime(confirmed=False)
        self.assertFalse(result["ok"])
        self.assertIn("confirmed=true", result["error"])

    async def test_cc_install_sandbox_requires_confirmed_true(self) -> None:
        settings = _settings()
        with patch.object(cc_like, "get_or_init_settings", AsyncMock(return_value=settings)):
            raw = await cc_like.cc_install_sandbox({"confirmed": False})
        payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        self.assertIn("confirmed=true", payload["error"])


if __name__ == "__main__":
    unittest.main()
