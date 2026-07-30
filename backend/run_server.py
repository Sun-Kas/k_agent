"""Command-line entry point for the private Agent Backend process."""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from backend.config import get_or_init_settings


def main() -> None:
    """启动对应的 FastAPI 服务进程。"""
    settings = asyncio.run(get_or_init_settings())
    parser = argparse.ArgumentParser(description="Run the stateless Agent Backend service.")
    parser.add_argument("--reload", action="store_true", default=settings.reload)
    parser.add_argument("--workers", "-w", type=int, default=settings.server_workers)
    args = parser.parse_args()
    uvicorn.run(
        "backend.main:create_app",
        host=settings.agent_backend_host,
        port=settings.agent_backend_port,
        reload=args.reload,
        workers=args.workers,
        factory=True,
    )


if __name__ == "__main__":
    main()
