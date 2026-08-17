from __future__ import annotations

import asyncio
import sys
from functools import lru_cache
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.core.config import Settings


if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    # Psycopg 异步连接在 Windows 上不支持默认 ProactorEventLoop。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_POSTGRES_POOL = None
_POSTGRES_SAVER = None
_POSTGRES_IDENTITY: tuple[str, bytes] | None = None
_POSTGRES_LOCK = asyncio.Lock()


@lru_cache(maxsize=1)
def _memory_saver() -> InMemorySaver:
    return InMemorySaver()


async def initialize_checkpointer(settings: Settings, *, setup: bool | None = None) -> None:
    provider = settings.langgraph_checkpointer.strip().lower()
    if provider != "postgres":
        return

    database_url = settings.langgraph_checkpoint_database_url.strip()
    key = settings.langgraph_aes_key.encode("utf-8")
    if not database_url:
        raise ValueError("LANGGRAPH_CHECKPOINT_DATABASE_URL 不能为空")
    if len(key) not in {16, 24, 32}:
        raise ValueError("LANGGRAPH_AES_KEY 必须为 16、24 或 32 字节")

    global _POSTGRES_POOL, _POSTGRES_SAVER, _POSTGRES_IDENTITY
    identity = (database_url, key)
    async with _POSTGRES_LOCK:
        if _POSTGRES_SAVER is not None:
            if _POSTGRES_IDENTITY != identity:
                raise RuntimeError("进程内禁止切换 LangGraph Checkpointer 数据库或加密密钥")
            return

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        serde = _encrypted_serde(settings)
        pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=max(1, settings.langgraph_checkpoint_pool_min_size),
            max_size=max(
                max(1, settings.langgraph_checkpoint_pool_min_size),
                settings.langgraph_checkpoint_pool_max_size,
            ),
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            name="mindbridge-langgraph-checkpoints",
        )
        await pool.open(wait=True)
        saver = AsyncPostgresSaver(pool, serde=serde)
        should_setup = settings.langgraph_checkpoint_auto_setup if setup is None else setup
        if should_setup:
            await saver.setup()
        _POSTGRES_POOL = pool
        _POSTGRES_SAVER = saver
        _POSTGRES_IDENTITY = identity


async def close_checkpointer() -> None:
    global _POSTGRES_POOL, _POSTGRES_SAVER, _POSTGRES_IDENTITY
    async with _POSTGRES_LOCK:
        pool = _POSTGRES_POOL
        _POSTGRES_POOL = None
        _POSTGRES_SAVER = None
        _POSTGRES_IDENTITY = None
        if pool is not None:
            await pool.close()


async def setup_checkpointer(settings: Settings) -> None:
    await initialize_checkpointer(settings, setup=True)


async def delete_checkpoint_threads(settings: Settings, thread_ids) -> int:
    ids = tuple(sorted({str(item) for item in thread_ids if str(item)}))
    if not ids or settings.langgraph_checkpointer.strip().lower() != "postgres":
        return 0
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(
        settings.langgraph_checkpoint_database_url,
        serde=_encrypted_serde(settings),
    ) as saver:
        for thread_id in ids:
            await saver.adelete_thread(thread_id)
    return len(ids)


async def prune_expired_checkpoints(settings: Settings) -> int:
    ttl = settings.langgraph_checkpoint_ttl_seconds
    if not ttl or ttl <= 0 or settings.langgraph_checkpointer.strip().lower() != "postgres":
        return 0
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(
        settings.langgraph_checkpoint_database_url,
        row_factory=dict_row,
    ) as conn:
        rows = await (
            await conn.execute(
                """
                SELECT thread_id
                FROM checkpoints
                GROUP BY thread_id
                HAVING MAX((checkpoint->>'ts')::timestamptz)
                    < NOW() - (%s * INTERVAL '1 second')
                """,
                (int(ttl),),
            )
        ).fetchall()
    return await delete_checkpoint_threads(
        settings,
        (row["thread_id"] for row in rows),
    )


def create_checkpointer(settings: Settings) -> Any:
    provider = settings.langgraph_checkpointer.strip().lower()
    if provider in {"", "disabled", "none"}:
        return False
    if provider == "memory":
        if settings.app_environment.strip().lower() not in {"test", "local", "development", "dev"}:
            raise ValueError("生产环境禁止使用 InMemorySaver；请配置 AsyncPostgresSaver。")
        return _memory_saver()
    if provider == "postgres":
        if _POSTGRES_SAVER is None:
            raise RuntimeError("AsyncPostgresSaver 尚未初始化，请先执行应用 startup 或 checkpoint setup")
        return _POSTGRES_SAVER
    raise ValueError(f"不支持的 LangGraph Checkpointer：{provider}")


def checkpointer_status(settings: Settings) -> dict[str, Any]:
    provider = settings.langgraph_checkpointer.strip().lower()
    enabled = provider not in {"", "disabled", "none"}
    durable = provider == "postgres"
    return {
        "provider": provider,
        "enabled": enabled,
        "initialized": provider == "memory" or (provider == "postgres" and _POSTGRES_SAVER is not None),
        "encrypted": provider == "postgres" and bool(settings.langgraph_aes_key),
        "durable": durable,
        "restartRecovery": durable,
    }


def _encrypted_serde(settings: Settings):
    from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    key = settings.langgraph_aes_key.encode("utf-8")
    if len(key) not in {16, 24, 32}:
        raise ValueError("LANGGRAPH_AES_KEY 必须为 16、24 或 32 字节")
    return EncryptedSerializer.from_pycryptodome_aes(
        serde=JsonPlusSerializer(
            pickle_fallback=False,
            allowed_msgpack_modules=(),
        ),
        key=key,
    )
