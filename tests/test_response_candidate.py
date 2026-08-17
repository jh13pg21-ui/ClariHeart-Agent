import unittest
from types import SimpleNamespace

from app.agents.autonomous import AgentNodeContext, ResponseAgent, SafetyAgent
from app.agents.events import AgentArtifact, AgentEventType
from app.core.enums import RiskLevel
from app.schemas.dtos import AiMessage
from app.services.output_safety import OutputSafetyStatus


class FakeClient:
    def __init__(self, text="安全候选回复。", chunks=None):
        self.text = text
        self.chunks = chunks or []
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return self.text

    async def stream(self, messages, **kwargs):
        for chunk in self.chunks:
            yield chunk


class EmptyPrivateMemory:
    def _key(self, agent_name, session_id):
        return f"{agent_name}:{session_id}"

    def load(self, agent_name, session_id):
        return []

    def append(self, agent_name, session_id, content):
        return None


def services(client, sink=None):
    profile = SimpleNamespace(provider="mock", model="mock", max_tokens=512)
    settings = SimpleNamespace(
        prompt_registry_enabled=False,
        context_planner_enabled=False,
        context_planner_shadow_mode=False,
        model_cloud_fallback_enabled=True,
        ai_max_tokens=512,
    )
    return SimpleNamespace(
        settings=settings,
        user=SimpleNamespace(id=1, display_name="同学"),
        session=SimpleNamespace(public_id="session"),
        private_memory=EmptyPrivateMemory(),
        model_registry=SimpleNamespace(client_for=lambda name: client, profile_for=lambda name: profile),
        response_token_sink=sink,
        long_term_memory=SimpleNamespace(mark_used_ids=lambda *args: None),
    )


def context(risk=RiskLevel.LOW, *extra):
    base = (
        AgentArtifact("memory", "ContextAgent", "memory", {"history": [], "memoryBrief": "无"}),
        AgentArtifact("intent", "UnderstandingAgent", "intent", {"intent": "CHAT"}),
        AgentArtifact("risk", "SafetyAgent", "risk", {"risk": risk.value}),
    )
    return AgentNodeContext(turn_id="turn", session_id="session", model_input="你好", artifacts=(*base, *extra))


class ResponseCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_contains_provider_metadata_without_prompt_messages(self):
        result = await ResponseAgent(services(FakeClient())).run(context())
        candidate = result.artifacts[0]
        self.assertEqual(candidate.kind, "response_candidate")
        self.assertEqual(candidate.payload["provider"], "mock")
        self.assertNotIn("messages", candidate.payload)
        self.assertRegex(candidate.metadata["promptSummaryHash"], r"^[0-9a-f]{64}$")

    async def test_low_risk_stream_forwards_only_reviewed_segments(self):
        forwarded = []

        async def sink(chunk):
            forwarded.append(chunk)

        client = FakeClient(chunks=["可以先这样做。", "后台风险评分：HIGH。"])
        result = await ResponseAgent(services(client, sink)).run(context())
        self.assertEqual(forwarded, ["可以先这样做。"])
        self.assertIn("后台风险评分", result.artifacts[0].payload["text"])

    async def test_safety_review_matches_candidate_id_and_requests_revision(self):
        candidate = AgentArtifact(
            "candidate",
            "ResponseAgent",
            "response_candidate",
            {"text": "后台风险评分：HIGH。", "revisionCount": 0},
        )
        node_context = context(RiskLevel.LOW, candidate)
        result = SafetyAgent(services(FakeClient())).review(node_context, candidate)
        review = result.artifacts[0]
        self.assertEqual(review.payload["status"], OutputSafetyStatus.REVISE.value)
        self.assertEqual(review.metadata["responseArtifactId"], candidate.id)
        self.assertEqual(result.events[0].type, AgentEventType.REVISION_REQUESTED)

    async def test_second_revision_fails_closed(self):
        candidate = AgentArtifact(
            "candidate",
            "ResponseAgent",
            "response_candidate",
            {"text": "后台风险评分：HIGH。", "revisionCount": 1},
        )
        result = SafetyAgent(services(FakeClient())).review(context(RiskLevel.LOW, candidate), candidate)
        self.assertEqual(result.artifacts[0].payload["status"], OutputSafetyStatus.FALLBACK.value)


if __name__ == "__main__":
    unittest.main()
