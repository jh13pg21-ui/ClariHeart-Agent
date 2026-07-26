import unittest
from types import SimpleNamespace

from app.agents.autonomous import ResponseAgent, SafetyAgent
from app.agents.events import AgentArtifact, AgentTask, CollaborationBlackboard
from app.core.enums import RiskLevel
from app.schemas.dtos import AiMessage
from app.services.output_safety import OutputSafetyStatus


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        return self.text


class FakePrivateMemory:
    def _key(self, agent_name, session_id):
        return f"{agent_name}:{session_id}"

    def load(self, agent_name, session_id):
        return []

    def append(self, agent_name, session_id, content):
        return None


def services(client):
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
