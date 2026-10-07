"""会话交互终端：同一会话可以有多块终端，每块一个 PTY。

浏览器收起底栏时连接保持，进程继续跑。某一块的连接断开、换会话或服务关闭时，
只结束对应进程。不写入会话事件，也不经过 Agent Backend。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import pty
import re
import shlex
import shutil
import signal
import socket
import struct
import tempfile
import termios
import fcntl
from pathlib import Path
from typing import Any

from fastapi import WebSocket

# 一块标签一个 id，由浏览器生成。限制字符，避免把路径拼进进程表的键。
TERMINAL_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_TERMINALS_PER_SESSION = 8


def shell_host_name() -> str:
    """提示符 @ 后面用机器名。本进程的 HOST 常是 127.0.0.1，原样传给 zsh 会显示成 127。"""
    name = socket.gethostname().split(".")[0].strip()
    if name and not name.isdigit() and not name.startswith("127"):
        return name
    return "localhost"


def prepare_private_shell_home() -> Path:
    """给这块终端单独的启动目录，避免向上键翻出别的窗口写进 ~/.zsh_history 的命令。"""
    root = Path(tempfile.mkdtemp(prefix="k-agent-shell-"))
    os.chmod(root, 0o700)
    history = root / "history"
    history.touch()
    home = Path.home()

    def source(name: str) -> str:
        target = shlex.quote(str(home / name))
        return f"if [ -f {target} ]; then . {target}; fi\n"

    (root / ".zshenv").write_text(source(".zshenv"), encoding="utf-8")
    (root / ".zprofile").write_text(source(".zprofile"), encoding="utf-8")
    (root / ".zshrc").write_text(source(".zshrc"), encoding="utf-8")
    # /etc/zshrc 会把 HISTFILE 指回用户主目录。这是登录流程里最后读到的文件，在这里改掉。
    (root / ".zlogin").write_text(
        source(".zlogin")
        + "unsetopt SHARE_HISTORY INC_APPEND_HISTORY INC_APPEND_HISTORY_TIME\n"
        + f"HISTFILE={shlex.quote(str(history))}\n",
        encoding="utf-8",
    )
    return root


def encode_message(kind: str, **fields: Any) -> str:
    """把一条终端消息编码成文本帧。input/output 的 data 是原始字节的 base64。"""
    payload: dict[str, Any] = {"type": kind, **fields}
    return json.dumps(payload, separators=(",", ":"))


def encode_bytes_message(kind: str, data: bytes) -> str:
    return encode_message(kind, data=base64.b64encode(data).decode("ascii"))


def decode_client_message(text: str) -> dict[str, Any]:
    """解析浏览器发来的 input / resize / close。非法帧直接拒绝。"""
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Terminal message must be an object")
    kind = payload.get("type")
    if kind == "input":
        raw = payload.get("data")
        if not isinstance(raw, str):
            raise ValueError("Terminal input data must be base64 text")
        return {"type": "input", "data": base64.b64decode(raw, validate=True)}
    if kind == "resize":
        cols = int(payload.get("cols"))
        rows = int(payload.get("rows"))
        if not 2 <= cols <= 500 or not 2 <= rows <= 200:
            raise ValueError("Terminal size is out of range")
        return {"type": "resize", "cols": cols, "rows": rows}
    if kind == "close":
        return {"type": "close"}
    raise ValueError(f"Unknown terminal message type: {kind}")


def set_pty_size(fd: int, rows: int, cols: int) -> None:
    """把浏览器里的行列写进 PTY，全屏程序才能按窗口重绘。"""
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


class SessionTerminal:
    """一个会话的登录 shell。多个连接共享同一 PTY，最后一个断开后关闭。"""

    def __init__(self, session_id: str, cwd: Path) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.clients: set[WebSocket] = set()
        self.fd: int | None = None
        self.pid: int | None = None
        self.closed = False
        self.shell_name = "zsh"
        self._private_home: Path | None = None
        self._send_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def cwd_label(self) -> str:
        return str(self.cwd)

    async def start(self) -> None:
        """fork 出登录 shell，工作目录固定为该会话工作区。"""
        self._loop = asyncio.get_running_loop()
        shell = os.environ.get("SHELL") or "/bin/zsh"
        # 标签上的名字就是这个程序名，例如 zsh。
        self.shell_name = Path(shell).name or "zsh"
        self._private_home = prepare_private_shell_home()
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                env["HOST"] = shell_host_name()
                env["ZDOTDIR"] = str(self._private_home)
                env["HISTFILE"] = str(self._private_home / "history")
                os.execvpe(shell, [shell, "-l"], env)
            finally:
                os._exit(1)
        self.pid = pid
        self.fd = fd
        os.set_blocking(fd, False)
        set_pty_size(fd, 24, 80)
        self._loop.add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        fd = self.fd
        loop = self._loop
        if fd is None or loop is None or self.closed:
            return
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                data = b""
            if not data:
                loop.create_task(self._finish(0))
                return
            chunks.append(data)
        if chunks:
            loop.create_task(self._broadcast(encode_bytes_message("output", b"".join(chunks))))

    async def _broadcast(self, text: str) -> None:
        async with self._send_lock:
            dead: list[WebSocket] = []
            for client in self.clients:
                try:
                    await client.send_text(text)
                except Exception:
                    dead.append(client)
            for client in dead:
                self.clients.discard(client)

    async def handle(self, websocket: WebSocket) -> None:
        """读一个连接上的按键和缩放，直到对端断开。"""
        while not self.closed:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not isinstance(text, str):
                continue
            try:
                payload = decode_client_message(text)
            except (ValueError, json.JSONDecodeError):
                continue
            if payload["type"] == "close":
                await self.close()
                return
            fd = self.fd
            if fd is None:
                return
            if payload["type"] == "input":
                try:
                    os.write(fd, payload["data"])
                except OSError:
                    await self._finish(0)
                    return
            elif payload["type"] == "resize":
                try:
                    set_pty_size(fd, payload["rows"], payload["cols"])
                except OSError:
                    pass

    async def _finish(self, code: int | None) -> None:
        if self.closed:
            return
        await self._broadcast(encode_message("exit", code=code))
        await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        fd = self.fd
        pid = self.pid
        loop = self._loop
        self.fd = None
        self.pid = None
        if fd is not None and loop is not None:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        if pid:
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass
        private_home = self._private_home
        self._private_home = None
        if private_home is not None:
            shutil.rmtree(private_home, ignore_errors=True)


class TerminalRegistry:
    """本进程内的会话终端表。SERVER_WORKERS 必须为 1，表不跨进程共享。"""

    def __init__(self) -> None:
        self._terminals: dict[str, SessionTerminal] = {}
        self._lock = asyncio.Lock()

    def _key(self, session_id: str, terminal_id: str) -> str:
        return f"{session_id}\n{terminal_id}"

    def _session_count(self, session_id: str) -> int:
        prefix = f"{session_id}\n"
        return sum(1 for key, terminal in self._terminals.items() if key.startswith(prefix) and not terminal.closed)

    async def attach(self, session_id: str, terminal_id: str, cwd: Path, websocket: WebSocket) -> None:
        key = self._key(session_id, terminal_id)
        async with self._lock:
            terminal = self._terminals.get(key)
            if terminal is None or terminal.closed:
                if self._session_count(session_id) >= MAX_TERMINALS_PER_SESSION:
                    await websocket.send_text(encode_message("exit", code=None))
                    await websocket.close(code=4409)
                    return
                terminal = SessionTerminal(session_id, cwd)
                await terminal.start()
                self._terminals[key] = terminal
            terminal.clients.add(websocket)
        await websocket.send_text(encode_message("ready", cwd=terminal.cwd_label, shell=terminal.shell_name))
        try:
            await terminal.handle(websocket)
        finally:
            terminal.clients.discard(websocket)
            if not terminal.clients:
                await terminal.close()
                async with self._lock:
                    if self._terminals.get(key) is terminal:
                        self._terminals.pop(key, None)

    async def close_all(self) -> None:
        async with self._lock:
            terminals = list(self._terminals.values())
            self._terminals.clear()
        for terminal in terminals:
            await terminal.close()
