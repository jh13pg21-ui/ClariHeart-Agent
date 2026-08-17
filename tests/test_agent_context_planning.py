import unittest
from types import SimpleNamespace

from app.agents.autonomous import AgentNodeContext, ResponseAgent
from app.agents.events import AgentArtifact
from app.core.enums import RiskLevel
from app.llm.errors import ModelError, ModelErrorCode
from app.schemas.dtos import AiMessage


class CapturingClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            raise ModelError(ModelErrorCode.PROMPT_TOO_LONG, "prompt too long", True, "mock", "tiny")
        return "候选答复"


class EmptyPrivateMemory:
    def _key(self, agent_name, session_id): return f"{agent_name}:{session_id}"
    def load(self, agent_name, session_id): return []
    def append(self, agent_name, session_id, content): return None


def services(client, window=4000):
    profile = SimpleNamespace(provider="mock", model="tiny-model", max_tokens=100)
    settings = SimpleNamespace(
        prompt_registry_enabled=True,
        context_planner_enabled=True,
        context_planner_shadow_mode=False,
        model_context_window_default=window,
        model_recovery_reserve_tokens=50,
        model_provider_safety_margin_ratio=0,
        model_token_estimator_safety_multiplier=1.0,
        model_cloud_fallback_enabled=True,
        ai_max_tokens=100,
    )
    return SimpleNamespace(
        settings=settings,
        user=SimpleNamespace(id=7, display_name="同学"),
        session=SimpleNamespace(public_id="session"),
        private_memory=EmptyPrivateMemory(),
        model_registry=SimpleNamespace(client_for=lambda name: client, profile_for=lambda name: profile),
        response_token_sink=None,
        long_term_memory=SimpleNamespace(mark_used_ids=lambda *args: None),
    )


def node_context(risk=RiskLevel.MEDIUM, reactive=False, injection=""):
    history = [AiMessage(role="user", content=f"旧问题{i}" * 20) for i in range(12)]
    artifacts = (
        AgentArtifact("intent", "UnderstandingAgent", "intent", {"intent": "CONSULT"}),
        AgentArtifact("risk", "SafetyAgent", "risk", {"risk": risk.value}),
        AgentArtifact(
            "context",
            "ContextAgent",
            "context",
            {
                "modelHistory": [*history, AiMessage(role="user", content="当前问题")],
                "memoryBrief": (injection or "历史摘要") * 80,
                "longTermMemoryContext": "长期背景" * 80,
                "longTermMemories": [],
                "retrievedKnowledge": [],
                "skillContext": "支持技能" * 40,
            },
            metadata={"reactiveCompacted": True} if reactive else {},
        ),
    )
    return AgentNodeContext(turn_id="turn", session_id="session", model_input="当前问题", artifacts=artifacts)


class AgentContextPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_is_within_selected_model_budget(self):
        result = await ResponseAgent(services(CapturingClient(), 1200)).run(node_context())
        candidate = result.artifacts[0]
        self.assertLessEqual(candidate.metadata["contextTokensAfter"], int(candidate.metadata["inputBudget"] * 0.95))
        self.assertTrue(candidate.metadata["contextPlanHash"])

    async def test_high_risk_never_allows_cloud(self):
        client = CapturingClient()
        await ResponseAgent(services(client)).run(node_context(RiskLevel.HIGH))
        self.assertFalse(client.calls[0][1]["cloud_egress_allowed"])
        self.assertEqual(client.calls[0][1]["risk_level"], RiskLevel.HIGH)

    async def test_reactive_plan_uses_emergency_allowlist(self):
        client = CapturingClient()
        result = await ResponseAgent(services(client)).run(node_context(reactive=True))
        self.assertTrue(result.artifacts[0].metadata["contextReactive"])
        self.assertNotIn("memory.long_term", client.calls[0][1]["context_section_ids"])

    async def test_prompt_too_long_is_rethrown_to_graph_recovery(self):
        with self.assertRaises(ModelError) as raised:
            await ResponseAgent(services(CapturingClient(fail=True))).run(node_context())
        self.assertEqual(raised.exception.code, ModelErrorCode.PROMPT_TOO_LONG)


if __name__ == "__main__":
    unittest.main()
