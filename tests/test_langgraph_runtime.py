import json
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.autonomous import ResponseAgent
from app.core.config import Settings
from app.core.database import Base
from app.core.enums import RiskLevel
from app.graph.checkpoint import checkpointer_status, create_checkpointer
from app.graph.nodes import finalize
from app.graph.routing import route_context, route_review
from app.graph.runtime import LangGraphAgentRuntime
from app.graph.state import initial_agent_state
from app.graph.tracing import configure_langsmith
from app.llm.errors import ModelError, ModelErrorCode
from app.models.entities import ChatSession, UserAccount


class LangGraphContractTests(unittest.TestCase):
    def test_state_only_contains_sanitized_model_input_contract(self):
        state = initial_agent_state(
            turn_id="turn-1",
            user_id=1,
            session_id="session-1",
            model_input="电话 [已脱敏]",
        )

        serialized = json.dumps(state, ensure_ascii=False)

        self.assertNotIn("original_input", state)
        self.assertNotIn("user_input", state)
        self.assertIn("[已脱敏]", serialized)

    def test_context_and_review_routes_are_explicit(self):
        state = initial_agent_state(turn_id="t", user_id=1, session_id="s", model_input="x")
        state["intent"] = {"payload": {"intent": "CONSULT"}}
        state["risk"] = {"payload": {"risk": "LOW"}}
        self.assertEqual(route_context(state), "gather_context")

        state["output_safety"] = {"payload": {"status": "REVISE"}}
        state["revision_count"] = 1
        self.assertEqual(route_review(state), "generate_response")
        state["revision_count"] = 2
        self.assertEqual(route_review(state), "safe_fallback")

    def test_finalize_fails_closed_when_review_does_not_match_candidate(self):
        state = initial_agent_state(turn_id="t", user_id=1, session_id="s", model_input="x")
        state["response_candidate"] = {"id": "candidate-1", "payload": {"text": "unsafe"}}
        state["output_safety"] = {
            "metadata": {"responseArtifactId": "other"},
            "payload": {"status": "APPROVED", "text": "unsafe"},
        }

        result = finalize(state)

        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotEqual(result["final_artifact_id"], "candidate-1")
        self.assertEqual(result["output_safety"]["payload"]["status"], "FALLBACK")

    def test_langgraph_is_the_only_runtime(self):
        graph_settings = Settings(_env_file=None)
        self.assertEqual(LangGraphAgentRuntime.framework_name, "langgraph")
        self.assertFalse(checkpointer_status(graph_settings)["durable"])
        self.assertFalse(checkpointer_status(graph_settings)["enabled"])

    def test_production_rejects_in_memory_checkpointer(self):
        settings = Settings(
            _env_file=None,
            app_environment="production",
            langgraph_checkpointer="memory",
        )

        with self.assertRaisesRegex(ValueError, "生产环境禁止使用 InMemorySaver"):
            create_checkpointer(settings)

    def test_langsmith_defaults_hide_graph_inputs_and_outputs(self):
        settings = Settings(
            _env_file=None,
            langsmith_tracing_enabled=True,
            langsmith_allow_content=False,
        )

        with patch.dict(os.environ, {}, clear=False):
            configure_langsmith(settings)

            self.assertEqual(os.environ["LANGSMITH_TRACING"], "true")
            self.assertEqual(os.environ["LANGSMITH_HIDE_INPUTS"], "true")
            self.assertEqual(os.environ["LANGSMITH_HIDE_OUTPUTS"], "true")


class LangGraphRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.settings = Settings(
            _env_file=None,
            app_environment="test",
            database_url="sqlite:///:memory:",
            ai_provider="mock",
            redis_url="redis://127.0.0.1:1/0",
            redis_socket_timeout_seconds=0.01,
            knowledge_vector_enabled=False,
            chroma_persist_dir=self.tempdir.name,
            langgraph_node_timeout_seconds=5,
            langgraph_node_max_attempts=2,
            langgraph_checkpointer="memory",
        )
        self.user = UserAccount(
            username="student",
            display_name="同学",
            password_hash="test",
            roles_csv="ROLE_USER",
        )
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(public_id="session-1", title="test", user_id=self.user.id)
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    async def test_low_risk_turn_runs_through_graph_and_preserves_result_contract(self):
        runtime = LangGraphAgentRuntime(self.db, self.settings)
        self.assertIsInstance(runtime, LangGraphAgentRuntime)

        try:
            result = await runtime.run(
                self.user,
                self.session,
                "你好",
                "你好",
            )
        finally:
            await runtime.ai.aclose()

        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertTrue(result.response_text)
        self.assertIn(result.output_safety.status.value, {"APPROVED", "FALLBACK"})
        self.assertTrue(any(step.action == "skip_context" for step in result.steps))
        self.assertTrue(any(step.action == "finalize" for step in result.steps))

        checkpoint = await create_checkpointer(self.settings).aget_tuple(
            {"configurable": {"thread_id": result.turn_id}}
        )
        channel_values = checkpoint.checkpoint["channel_values"]
        for sensitive_key in (
            "memory",
            "context",
            "response_candidate",
            "output_safety",
            "final_response",
        ):
            self.assertNotIn(sensitive_key, channel_values)
        self.assertTrue(all("payload" not in item for item in channel_values["artifacts"]))

    async def test_high_risk_turn_remains_high_and_returns_reviewed_text(self):
        runtime = LangGraphAgentRuntime(self.db, self.settings)
        try:
            result = await runtime.run(
                self.user,
                self.session,
                "我不想活了",
                "我不想活了",
            )
        finally:
            await runtime.ai.aclose()

        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertTrue(result.response_text)
        self.assertEqual(result.response_text, result.output_safety.text)
        self.assertTrue(any(event.type.value == "SAFETY_OVERRIDE" for event in result.collaboration_events))

    async def test_langgraph_custom_stream_forwards_reviewed_low_risk_tokens(self):
        runtime = LangGraphAgentRuntime(self.db, self.settings)
        tokens = []

        async def sink(token):
            tokens.append(token)

        try:
            result = await runtime.run(
                self.user,
                self.session,
                "你好",
                "你好",
                response_token_sink=sink,
            )
        finally:
            await runtime.ai.aclose()

        self.assertTrue(tokens)
        self.assertEqual("".join(tokens).strip(), result.response_text)

    async def test_prompt_overflow_compacts_once_then_regenerates(self):
        runtime = LangGraphAgentRuntime(self.db, self.settings)
        original_run = ResponseAgent.run
        calls = 0

        async def flaky_run(agent, context, *, revision_count=0, revision_of=""):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelError(
                    code=ModelErrorCode.PROMPT_TOO_LONG,
                    message="prompt too long",
                    retryable=True,
                    provider="mock",
                    model="mock",
                )
            return await original_run(
                agent,
                context,
                revision_count=revision_count,
                revision_of=revision_of,
            )

        try:
            with patch.object(ResponseAgent, "run", new=flaky_run):
                result = await runtime.run(
                    self.user,
                    self.session,
                    "最近压力很大",
                    "最近压力很大",
                )
        finally:
            await runtime.ai.aclose()

        self.assertEqual(calls, 2)
        self.assertTrue(result.response_text)
        self.assertEqual(
            sum(event.type.value == "CONTEXT_COMPACTED" for event in result.collaboration_events),
            1,
        )

    async def test_second_prompt_overflow_routes_to_deterministic_fallback(self):
        runtime = LangGraphAgentRuntime(self.db, self.settings)
        calls = 0

        async def overflowing_run(agent, context, *, revision_count=0, revision_of=""):
            nonlocal calls
            calls += 1
            raise ModelError(
                code=ModelErrorCode.PROMPT_TOO_LONG,
                message="prompt still too long",
                retryable=True,
                provider="mock",
                model="mock",
            )

        try:
            with patch.object(ResponseAgent, "run", new=overflowing_run):
                result = await runtime.run(
                    self.user,
                    self.session,
                    "最近压力很大",
                    "最近压力很大",
                )
        finally:
            await runtime.ai.aclose()

        self.assertEqual(calls, 2)
        self.assertEqual(result.output_safety.status.value, "FALLBACK")
        self.assertEqual(
            sum(event.type.value == "CONTEXT_UNRECOVERABLE" for event in result.collaboration_events),
            1,
        )


if __name__ == "__main__":
    unittest.main()
