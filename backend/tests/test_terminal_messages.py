"""终端消息编解码。PTY 本身依赖本机伪终端，不在这里拉起登录 shell。"""

from __future__ import annotations

import json
import shlex
import shutil
import unittest
from pathlib import Path
from unittest import mock

from access_layer.sessions.terminal import (
    TERMINAL_ID_RE,
    decode_client_message,
    encode_bytes_message,
    encode_message,
    prepare_private_shell_home,
    shell_host_name,
)


class TerminalMessageTests(unittest.TestCase):
    def test_round_trip_input_bytes(self) -> None:
        encoded = encode_bytes_message("input", b"ls\n\xff")
        decoded = decode_client_message(encoded)
        self.assertEqual(decoded, {"type": "input", "data": b"ls\n\xff"})

    def test_resize_bounds(self) -> None:
        decoded = decode_client_message(encode_message("resize", cols=80, rows=24))
        self.assertEqual(decoded, {"type": "resize", "cols": 80, "rows": 24})
        with self.assertRaises(ValueError):
            decode_client_message(json.dumps({"type": "resize", "cols": 1, "rows": 24}))

    def test_close_message(self) -> None:
        self.assertEqual(decode_client_message(encode_message("close")), {"type": "close"})

    def test_shell_host_name_skips_loopback(self) -> None:
        with mock.patch("access_layer.sessions.terminal.socket.gethostname", return_value="127.0.0.1"):
            self.assertEqual(shell_host_name(), "localhost")
        with mock.patch("access_layer.sessions.terminal.socket.gethostname", return_value="KANEMA-MC0.local"):
            self.assertEqual(shell_host_name(), "KANEMA-MC0")

    def test_private_shell_home_overrides_shared_history(self) -> None:
        root = prepare_private_shell_home()
        try:
            zlogin = (root / ".zlogin").read_text(encoding="utf-8")
            self.assertIn(str(Path.home() / ".zlogin"), zlogin)
            self.assertIn(f"HISTFILE={shlex.quote(str(root / 'history'))}", zlogin)
            self.assertIn(str(Path.home() / ".zshrc"), (root / ".zshrc").read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(root)

    def test_terminal_id_shape(self) -> None:
        self.assertIsNotNone(TERMINAL_ID_RE.fullmatch("3f1c0a2e-6b7d-4e11-9a55-0c1d2e3f4a5b"))
        self.assertIsNone(TERMINAL_ID_RE.fullmatch("../etc"))
        self.assertIsNone(TERMINAL_ID_RE.fullmatch("short"))


if __name__ == "__main__":
    unittest.main()
