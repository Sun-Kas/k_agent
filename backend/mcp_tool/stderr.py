"""Line-oriented bridge from MCP child stderr into k_agent logs.

stdio MCP servers (especially Node ones) often dump full HTTP/axios objects to
stderr. Forwarding that stream raw makes the Agent Backend terminal unreadable,
so we keep only short, non-dump lines and report how many noisy lines we hid.

The MCP SDK passes ``errlog`` to ``anyio.open_process(stderr=...)``, which
requires a real file descriptor. This bridge therefore owns an ``os.pipe``:
the write end is exposed via ``fileno()`` for the child, and a daemon thread
reads the read end into the filter.
"""

from __future__ import annotations

import os
import threading
import time
from io import TextIOBase

from backend.logging_config import log_event


_NOISE_MARKERS = (
    "[Symbol(",
    "IncomingMessage",
    "_readableState",
    "Object: null prototype",
    "TLSSocket",
    "ClientRequest",
    "HTTPParser",
    "kOutHeaders",
    "kHeaders",
    "kCapture",
    "shapeMode",
    "nodejs",
    "    at ",
)


class McpStderrBridge(TextIOBase):
    """TextIO sink compatible with mcp.client.stdio.stdio_client(errlog=...)."""

    def __init__(
        self,
        server_id: str,
        *,
        max_line_chars: int = 240,
        max_lines_per_window: int = 12,
        window_seconds: float = 5.0,
    ) -> None:
        self._server_id = server_id
        self._max_line_chars = max_line_chars
        self._max_lines_per_window = max_lines_per_window
        self._window_seconds = window_seconds
        self._buffer = ""
        self._window_started = time.monotonic()
        self._emitted_in_window = 0
        self._suppressed_in_window = 0
        self._closed = False
        self._lock = threading.Lock()
        self._read_fd, self._write_fd = os.pipe()
        # Child stderr is redirected to the write end; keep it inheritable.
        try:
            os.set_inheritable(self._write_fd, True)
        except (AttributeError, OSError):
            pass
        self._pump = threading.Thread(
            target=self._pump_pipe,
            name=f"mcp-stderr-{server_id}",
            daemon=True,
        )
        self._pump.start()

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def closed(self) -> bool:
        return self._closed

    def writable(self) -> bool:
        return not self._closed

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed file")
        return self._write_fd

    def write(self, data: str) -> int:
        # Direct writes are used by unit tests; the child process uses fileno().
        if self._closed or not data:
            return 0
        with self._lock:
            self._buffer += data
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._handle_line(line.rstrip("\r"))
        return len(data)

    def flush(self) -> None:
        if self._closed:
            return
        with self._lock:
            if self._buffer.strip():
                self._handle_line(self._buffer.rstrip("\r"))
            self._buffer = ""
            self._flush_suppressed(force=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._write_fd)
        except OSError:
            pass
        if self._pump.is_alive():
            self._pump.join(timeout=2.0)
        with self._lock:
            if self._buffer.strip():
                self._handle_line(self._buffer.rstrip("\r"))
            self._buffer = ""
            self._flush_suppressed(force=True)

    def _pump_pipe(self) -> None:
        pending = b""
        try:
            while True:
                try:
                    chunk = os.read(self._read_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    text = raw.decode("utf-8", errors="replace").rstrip("\r")
                    with self._lock:
                        self._handle_line(text)
            if pending.strip():
                text = pending.decode("utf-8", errors="replace").rstrip("\r")
                with self._lock:
                    self._handle_line(text)
        finally:
            try:
                os.close(self._read_fd)
            except OSError:
                pass
            with self._lock:
                self._flush_suppressed(force=True)

    def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        self._roll_window()
        if self._is_noise(line, stripped):
            self._suppressed_in_window += 1
            return
        if self._emitted_in_window >= self._max_lines_per_window:
            self._suppressed_in_window += 1
            return
        self._flush_suppressed(force=False)
        self._emitted_in_window += 1
        text = stripped if len(stripped) <= self._max_line_chars else (
            stripped[: self._max_line_chars - 1] + "…"
        )
        log_event(
            "mcp.server.stderr",
            serverId=self._server_id,
            line=text,
        )

    def _roll_window(self) -> None:
        now = time.monotonic()
        if now - self._window_started < self._window_seconds:
            return
        self._flush_suppressed(force=True)
        self._window_started = now
        self._emitted_in_window = 0

    def _flush_suppressed(self, *, force: bool) -> None:
        if self._suppressed_in_window <= 0:
            return
        if not force and self._suppressed_in_window < 8:
            return
        count = self._suppressed_in_window
        self._suppressed_in_window = 0
        log_event(
            "mcp.server.stderr.suppressed",
            serverId=self._server_id,
            suppressedLines=count,
        )

    @staticmethod
    def _is_noise(raw: str, stripped: str) -> bool:
        if any(marker in raw for marker in _NOISE_MARKERS):
            return True
        # Pretty-printed object dumps are almost always indented continuation lines.
        if raw.startswith((" ", "\t")) and (
            stripped.endswith((",", "{", "}", "[", "]"))
            or ":" in stripped
        ):
            return True
        if stripped in {"{", "}", "[", "]", "},", "],"}:
            return True
        return False
