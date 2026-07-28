from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPT_DIR) in sys.path:
    sys.path.remove(str(SCRIPT_DIR))

from backend.config import get_or_init_settings


def main() -> None:
    settings = asyncio.run(get_or_init_settings())
    parser = argparse.ArgumentParser(description="Run the K Agent API server.")
    parser.add_argument("--reload", action="store_true", default=settings.reload)
    parser.add_argument("--workers", "-w", type=int, default=settings.server_workers)
    args = parser.parse_args()

    uvicorn.run(
        "access_layer.main:create_app",
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        workers=args.workers,
        factory=True,
    )


if __name__ == "__main__":
    main()
