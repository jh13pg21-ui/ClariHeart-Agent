import os
import unittest
import uuid
from typing import Annotated, TypedDict

from langgraph.channels import UntrackedValue
from langgraph.graph import END, START, StateGraph
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.core.config import Settings
from app.graph.checkpoint import (
    close_checkpointer,
    create_checkpointer,
    initialize_checkpointer,
    prune_expired_checkpoints,
)


POSTGRES_URL = os.getenv("LANGGRAPH_TEST_POSTGRES_URL", "")


class CheckpointState(TypedDict, total=False):
    safe_status: str
    secret_text: Annotated[str, UntrackedValue(str)]
    left_value: str
    right_value: str


@unittest.skipUnless(POSTGRES_URL, "未配置 LANGGRAPH_TEST_POSTGRES_URL")
class LangGraphPostgresCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await close_checkpointer()

    async def test_encrypted_postgres_checkpoint_survives_pool_restart_without_secret_state(self):
        settings = Settings(
            _env_file=None,
            app_environment="test",
            langgraph_checkpointer="postgres",
            langgraph_checkpoint_database_url=POSTGRES_URL,
            langgraph_aes_key="0123456789abcdef0123456789abcdef",
        )
        thread_id = f"checkpoint-test-{uuid.uuid4().hex}"

        await initialize_checkpointer(settings, setup=True)
        first_saver = create_checkpointer(settings)
        first_graph = self._graph(first_saver)
        await first_graph.ainvoke(
            {"safe_status": "RUNNING"},
            {"configurable": {"thread_id": thread_id}},
        )
        async with await AsyncConnection.connect(POSTGRES_URL, row_factory=dict_row) as conn:
            rows = await (
                await conn.execute(
                    "SELECT type, blob FROM checkpoint_blobs WHERE thread_id = %s",
                    (thread_id,),
                )
            ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(str(row["type"]).endswith("+aes") for row in rows))
        self.assertTrue(
            all("不得进入 checkpoint".encode("utf-8") not in bytes(row["blob"]) for row in rows)
        )
        await close_checkpointer()

        await initialize_checkpointer(settings)
        second_saver = create_checkpointer(settings)
        second_graph = self._graph(second_saver)
        snapshot = await second_graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )

        self.assertEqual(snapshot.values["safe_status"], "COMPLETED")
        self.assertNotIn("secret_text", snapshot.values)
        await second_saver.adelete_thread(thread_id)

    async def test_resume_does_not_repeat_successful_parallel_node(self):
        settings = Settings(
            _env_file=None,
            app_environment="test",
            langgraph_checkpointer="postgres",
            langgraph_checkpoint_database_url=POSTGRES_URL,
            langgraph_aes_key="0123456789abcdef0123456789abcdef",
        )
        thread_id = f"checkpoint-pending-{uuid.uuid4().hex}"
        calls = {"left": 0, "right": 0}
        allow_right = False

        def left(state):
            calls["left"] += 1
            return {"left_value": "done"}

        def right(state):
            calls["right"] += 1
            if not allow_right:
                raise RuntimeError("模拟并行节点进程中断")
            return {"right_value": "done"}

        def graph(saver):
            builder = StateGraph(CheckpointState)
            builder.add_node("left", left)
            builder.add_node("right", right)
            builder.add_node("join", lambda state: {"safe_status": "COMPLETED"})
            builder.add_edge(START, "left")
            builder.add_edge(START, "right")
            builder.add_edge(["left", "right"], "join")
            builder.add_edge("join", END)
            return builder.compile(checkpointer=saver)

        await initialize_checkpointer(settings, setup=True)
        first_saver = create_checkpointer(settings)
        with self.assertRaisesRegex(RuntimeError, "模拟并行节点进程中断"):
            await graph(first_saver).ainvoke(
                {"safe_status": "RUNNING"},
                {"configurable": {"thread_id": thread_id}},
            )
        await close_checkpointer()

        allow_right = True
        await initialize_checkpointer(settings)
        second_saver = create_checkpointer(settings)
        result = await graph(second_saver).ainvoke(
            None,
            {"configurable": {"thread_id": thread_id}},
        )

        self.assertEqual(result["safe_status"], "COMPLETED")
        self.assertEqual(calls["left"], 1)
        self.assertEqual(calls["right"], 2)
        await second_saver.adelete_thread(thread_id)

    async def test_ttl_prune_deletes_expired_thread(self):
        settings = Settings(
            _env_file=None,
            app_environment="test",
            langgraph_checkpointer="postgres",
            langgraph_checkpoint_database_url=POSTGRES_URL,
            langgraph_aes_key="0123456789abcdef0123456789abcdef",
            langgraph_checkpoint_ttl_seconds=1,
        )
        thread_id = f"checkpoint-expired-{uuid.uuid4().hex}"
        await initialize_checkpointer(settings, setup=True)
        saver = create_checkpointer(settings)
        await self._graph(saver).ainvoke(
            {"safe_status": "RUNNING"},
            {"configurable": {"thread_id": thread_id}},
        )
        async with await AsyncConnection.connect(POSTGRES_URL) as conn:
            await conn.execute(
                """
                UPDATE checkpoints
                SET checkpoint = jsonb_set(
                    checkpoint,
                    '{ts}',
                    to_jsonb('2000-01-01T00:00:00+00:00'::text)
                )
                WHERE thread_id = %s
                """,
                (thread_id,),
            )
            await conn.commit()
        await close_checkpointer()

        deleted = await prune_expired_checkpoints(settings)

        self.assertGreaterEqual(deleted, 1)
        await initialize_checkpointer(settings)
        self.assertIsNone(
            await create_checkpointer(settings).aget_tuple(
                {"configurable": {"thread_id": thread_id}}
            )
        )

    @staticmethod
    def _graph(saver):
        builder = StateGraph(CheckpointState)
        builder.add_node(
            "complete",
            lambda state: {
                "safe_status": "COMPLETED",
                "secret_text": "不得进入 checkpoint 的心理正文",
            },
        )
        builder.add_edge(START, "complete")
        builder.add_edge("complete", END)
        return builder.compile(checkpointer=saver)


if __name__ == "__main__":
    unittest.main()
