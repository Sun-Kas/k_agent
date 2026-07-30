from __future__ import annotations

import io
import os
import unittest

from backend.logging_config import configure_agent_backend_logging
from backend.mcp_tool.stderr import McpStderrBridge


class McpStderrBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = io.StringIO()
        configure_agent_backend_logging("INFO", stream=self.output)

    def test_keeps_short_errors_and_suppresses_object_dumps(self) -> None:
        bridge = McpStderrBridge(
            "12306-mcp",
            max_line_chars=80,
            max_lines_per_window=5,
            window_seconds=60.0,
        )
        try:
            bridge.write("Failed to fetch tickets: status 404\n")
            bridge.write("  status: 404\n")
            bridge.write("  res: IncomingMessage {\n")
            bridge.write("      [Symbol(kCapture)]: false,\n")
            bridge.write("      host: 'kyfw.12306.cn',\n")
            for _ in range(20):
                bridge.write("      _events: [Object],\n")
            bridge.flush()

            text = self.output.getvalue()
            self.assertIn("MCP[12306-mcp]", text)
            self.assertIn("Failed to fetch tickets: status 404", text)
            self.assertIn("已折叠噪声日志", text)
            self.assertNotIn("IncomingMessage", text)
            self.assertNotIn("Symbol(kCapture)", text)
        finally:
            bridge.close()

    def test_fileno_accepts_pipe_writes_from_child_stderr(self) -> None:
        bridge = McpStderrBridge("pipe-mcp", window_seconds=60.0)
        try:
            os.write(bridge.fileno(), b"connect timeout from child\n")
        finally:
            bridge.close()

        text = self.output.getvalue()
        self.assertIn("connect timeout from child", text)


if __name__ == "__main__":
    unittest.main()

