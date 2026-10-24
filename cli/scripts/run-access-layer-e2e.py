"""以真实 HTTP / WebSocket 验证 CLI；所有状态和 Git 操作限定于一次性目录。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))

with TemporaryDirectory(prefix="k-agent-cli-api-", dir="/private/tmp") as temporary:
    directory = Path(temporary)
    home, repository = directory / "home", directory / "repo"
    repository.mkdir()
    os.environ.update(
        K_AGENT_HOME=str(home), STORAGE_BASE_DIR=str(home / "state"),
        MCP_CONFIG_PATH=str(home / "config/mcp.json"),
        TEAM_RUNTIME_ENABLED="false", AGENT_BACKEND_URL="http://127.0.0.1:1",
    )
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "CLI Test")
    git("config", "user.email", "cli-test@example.invalid")
    git("config", "commit.gpgsign", "false")
    (repository / "baseline.txt").write_text("baseline\n")
    git("add", "baseline.txt")
    git("commit", "-m", "fixture baseline")

    import uvicorn
    from access_layer.main import create_app

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    fixture = directory / "fixture.json"
    fixture.write_text(json.dumps({"endpoint": f"http://127.0.0.1:{listener.getsockname()[1]}", "repo": str(repository)}))
    server = uvicorn.Server(uvicorn.Config(create_app(), log_level="error", access_log=False))
    worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        for _ in range(150):
            if server.started:
                break
            if not worker.is_alive():
                raise RuntimeError("一次性 API 服务启动失败")
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError("一次性 API 服务启动超时")
        subprocess.run(["node", str(root / "cli/scripts/test-access-layer-e2e.mjs"), str(fixture)], cwd=root, check=True, timeout=60)
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()
        if worker.is_alive():
            raise RuntimeError("一次性测试服务未正常退出")
