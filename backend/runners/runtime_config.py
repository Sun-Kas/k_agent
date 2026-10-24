"""单次 CLI 运行的临时配置目录，与用户工作树隔离。"""

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import AsyncIterator


@asynccontextmanager
async def runtime_config_dir() -> AsyncIterator[Path]:
    # TemporaryDirectory 使用私有目录权限，审批桥凭据不会进入项目或 Git Diff。
    with TemporaryDirectory(prefix="k-agent-runner-") as temporary:
        yield Path(temporary)
