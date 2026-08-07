import unittest
from types import SimpleNamespace

from app.agents.autonomous import ResponseAgent, SafetyAgent
from app.agents.events import AgentArtifact, AgentTask, CollaborationBlackboard
from app.core.enums import RiskLevel
from app.llm.errors import ModelError, ModelErrorCode
from app.schemas.dtos import AiMessage
from app.services.output_safety import OutputSafetyStatus


class FakeClient:
    def __init__(self, text, stream_chunks=None):
        self.text = text
        self.calls = 0
        self.stream_chunks = stream_chunks or [text]
        self.complete_kwargs = []
        self.stream_kwargs = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.complete_kwargs.append(kwargs)
        return self.text

    async def stream(self, messages, **kwargs):
        self.stream_kwargs.append(kwargs)
        for chunk in self.stream_chunks:
            yield chunk


class FakePrivateMemory:
    def _key(self, agent_name, session_id):
        return f"{agent_name}:{session_id}"

    def load(self, agent_name, session_id):
        return []

    def append(self, agent_name, session_id, content):
        return None


class FailingClient(FakeClient):
    def __init__(self, error):
        super().__init__("")
        self.error = error

    async def complete(self, messages, **kwargs):
        raise self.error


def services(client, response_token_sink=None):
    profile = SimpleNamespace(provider="mock", model="candidate-model")
    return SimpleNamespace(
        settings=SimpleNamespace(),
        user=SimpleNamespace(display_name="同学"),
        session=SimpleNamespace(public_id="session"),
        private_memory=FakePrivateMemory(),
        model_registry=SimpleNamespace(
            client_for=lambda name: client,
            profile_for=lambda name: profile,
        ),
        response_token_sink=response_token_sink,
    )


def base_board():
    return (
        CollaborationBlackboard(turn_id="turn", user_input="你好", model_input="你好")
        .add_artifact(
            AgentArtifact(
                id="intent",
                owner="UnderstandingAgent",
                kind="intent",
                payload={"intent": "CHAT"},
            )
        )
        .add_artifact(
            AgentArtifact(
                id="risk",
                owner="SafetyAgent",
                kind="risk",
                payload={"risk": "LOW"},
            )
        )
    )


class ResponseCandidateTests(unittest.IsolatedAsyncioTestCase):
    def _high_risk_board(self):
        return base_board().add_artifact(
            AgentArtifact(
                id="high-risk",
                owner="SafetyAgent",
                kind="risk",
                payload={"risk": "HIGH"},
            )
        ).add_artifact(
            AgentArtifact(
                id="context",
                owner="ContextAgent",
                kind="context",
                payload={"modelHistory": [AiMessage(role="user", content="我很危险")]},
            )
        )

    async def test_exhausted_model_error_publishes_typed_local_safety_fallback(self):
        client = FailingClient(
            ModelError(
                code=ModelErrorCode.OVERLOADED,
                message="local exhausted",
                retryable=True,
                provider="ollama",
                model="local-model",
            )
        )
        agent = ResponseAgent(services(client))

        result = await agent.act(
            AgentTask(id="response-high", title="response"),
            self._high_risk_board(),
        )

        candidate = result.artifacts[0]
        self.assertEqual(candidate.payload["generationStatus"], "fallback")
        self.assertEqual(candidate.payload["failureCode"], "OVERLOADED")
        self.assertEqual(candidate.payload["route"], "local_safety_fallback")
        self.assertTrue(candidate.payload["text"])

    async def test_unexpected_generation_error_reaches_task_recovery_boundary(self):
        client = FailingClient(ValueError("programming defect"))
        agent = ResponseAgent(services(client))

        with self.assertRaisesRegex(ValueError, "programming defect"):
            await agent.act(
                AgentTask(id="response-high", title="response"),
                self._high_risk_board(),
            )

    async def test_response_agent_passes_actual_risk_to_model_client(self):
        client = FakeClient("高风险安全答复。")
        agent = ResponseAgent(services(client))

        await agent.act(
            AgentTask(id="response-high", title="response"),
            self._high_risk_board(),
        )

        self.assertEqual(client.complete_kwargs[0]["risk_level"], RiskLevel.HIGH)
        self.assertFalse(client.complete_kwargs[0]["cloud_egress_allowed"])

    async def test_response_agent_generates_text_candidate_without_messages_payload(self):
        client = FakeClient("这是最终候选文本。")
        agent = ResponseAgent(services(client))

        result = await agent.act(AgentTask(id="response", title="response"), base_board())

        candidate = result.artifacts[0]
        self.assertEqual(candidate.kind, "response_candidate")
        self.assertEqual(candidate.payload["text"], "这是最终候选文本。")
        self.assertEqual(candidate.payload["model"], "candidate-model")
        self.assertEqual(candidate.payload["provider"], "mock")
        self.assertGreaterEqual(candidate.payload["latencyMs"], 0)
        self.assertNotIn("messages", candidate.payload)
        self.assertRegex(candidate.metadata["promptSummaryHash"], r"^[0-9a-f]{64}$")

    async def test_low_risk_response_uses_provider_stream_and_forwards_original_chunks(self):
        chunks = ["第一段", "，第二段", "。"]
        forwarded = []

        async def sink(chunk):
            forwarded.append(chunk)

        client = FakeClient("不应调用 complete", chunks)
        agent = ResponseAgent(services(client, response_token_sink=sink))

        result = await agent.act(AgentTask(id="response", title="response"), base_board())

        self.assertEqual(forwarded, ["".join(chunks)])
        self.assertEqual(result.artifacts[0].payload["text"], "第一段，第二段。")
        self.assertEqual(client.calls, 0)

    async def test_low_risk_stream_stops_before_exposing_output_safety_blocker(self):
        forwarded = []

        async def sink(chunk):
            forwarded.append(chunk)

        chunks = ["可以先这样做。", "后台风险评分：HIGH。", "不应继续推送。"]
        client = FakeClient("不应调用 complete", chunks)
        agent = ResponseAgent(services(client, response_token_sink=sink))

        result = await agent.act(AgentTask(id="response", title="response"), base_board())

        self.assertEqual(forwarded, ["可以先这样做。"])
        self.assertEqual(result.artifacts[0].payload["text"], "".join(chunks))

    async def test_safety_agent_reviews_actual_candidate_text(self):
        client = FakeClient("unused")
        agent = SafetyAgent(services(client))
        candidate = AgentArtifact(
            id="candidate",
            owner="ResponseAgent",
            kind="response_candidate",
            payload={"text": "后台风险评分：HIGH。", "revisionCount": 0},
        )
        board = base_board().add_artifact(candidate)

        result = await agent.act(AgentTask(id="review", title="review"), board)

        critique = result.artifacts[0]
        self.assertEqual(critique.kind, "output_safety")
        self.assertEqual(critique.payload["status"], OutputSafetyStatus.REVISE.value)
        self.assertEqual(len(result.tasks), 1)

    async def test_second_revision_request_becomes_fallback_instead_of_third_generation(self):
        client = FakeClient("unused")
        agent = SafetyAgent(services(client))
        candidate = AgentArtifact(
            id="candidate",
            owner="ResponseAgent",
            kind="response_candidate",
            payload={"text": "后台风险评分：HIGH。", "revisionCount": 1},
        )
        board = base_board().add_artifact(candidate)

        result = await agent.act(AgentTask(id="review", title="review"), board)

        review = result.artifacts[0]
        self.assertEqual(review.payload["status"], OutputSafetyStatus.FALLBACK.value)
        self.assertEqual(result.tasks, ())
        self.assertTrue(review.payload["text"])


if __name__ == "__main__":
    unittest.main()
