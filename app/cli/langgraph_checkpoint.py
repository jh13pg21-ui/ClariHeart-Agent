from __future__ import annotations

import argparse
import asyncio

from app.core.config import get_settings
from app.graph.checkpoint import (
    close_checkpointer,
    delete_checkpoint_threads,
    prune_expired_checkpoints,
    setup_checkpointer,
)


async def _setup() -> None:
    settings = get_settings()
    if settings.langgraph_checkpointer.strip().lower() != "postgres":
        raise SystemExit("LANGGRAPH_CHECKPOINTER 必须设置为 postgres")
    try:
        await setup_checkpointer(settings)
    finally:
        await close_checkpointer()


async def _prune() -> None:
    count = await prune_expired_checkpoints(get_settings())
    print(f"已删除 {count} 个过期 checkpoint thread")


async def _delete(thread_id: str) -> None:
    count = await delete_checkpoint_threads(get_settings(), (thread_id,))
    print(f"已删除 {count} 个 checkpoint thread")


def main() -> None:
    parser = argparse.ArgumentParser(description="管理 LangGraph checkpoint 存储")
    parser.add_argument("command", choices=("setup", "prune", "delete-thread"))
    parser.add_argument("thread_id", nargs="?")
    args = parser.parse_args()
    if args.command == "setup":
        asyncio.run(_setup())
    elif args.command == "prune":
        asyncio.run(_prune())
    elif args.command == "delete-thread":
        if not args.thread_id:
            parser.error("delete-thread 必须提供 thread_id")
        asyncio.run(_delete(args.thread_id))


if __name__ == "__main__":
    main()
