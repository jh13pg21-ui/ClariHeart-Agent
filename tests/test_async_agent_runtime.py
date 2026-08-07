import asyncio
import time
import unittest
from types import SimpleNamespace

import httpx

from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentTask,
    AgentTurnResult,
    CollaborationBlackboard,
)
from app.agents.registry import AgentDecision, AgentProfile, AgentRegistry
from app.llm.errors import ModelError, ModelErrorCode


class SleepingAgent:
    def __init__(self, name: str, artifact_kind: str):
        self.profile = AgentProfile(name=name)
        self.artifact_kind = artifact_kind
        self.round_boards = []

    def decide(self, task, board):
        return AgentDecision(task.id == f"task:{self.profile.name}", 1.0, "独立任务")

    async def act(self, task, board):
        self.round_boards.append(board)
        await asyncio.sleep(0.1)
        events = ()
        if self.artifact_kind == "safety_event":
            events = (AgentEvent(type=AgentEventType.SAFETY_OVERRIDE, actor=self.profile.name),)
        return AgentTurnResult(
            artifacts=(
                AgentArtifact(
                    id=f"artifact:{self.profile.name}",
                    owner=self.profile.name,
                    kind=self.artifact_kind,
                    payload={"risk": "LOW", "intent": "CHAT"},
                    task_id=task.id,
                ),
            ),
            events=events,
        )


class FailingAgent(SleepingAgent):
    def __init__(self, name: str, artifact_kind: str, failures: int, exc: Exception):
        super().__init__(name, artifact_kind)
        self.failures = failures
        self.exc = exc
        self.calls = 0

    async def act(self, task, board):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return await super().act(task, board)


class AsyncAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_independent_agents_run_concurrently_on_one_immutable_round_board(self):
        agents = [
            SleepingAgent("Safety", "safety_event"),
            SleepingAgent("Risk", "risk"),
            SleepingAgent("Intent", "intent"),
        ]
        board = CollaborationBlackboard(
            turn_id="turn",
            tasks={
                f"task:{agent.profile.name}": AgentTask(
                    id=f"task:{agent.profile.name}",
                    title=agent.profile.name,
                )
                for agent in agents
            },
        )
        settings = SimpleNamespace(
            agent_max_rounds=1,
            agent_max_claims_per_round=3,
            agent_max_claims_per_agent=1,
            agent_final_acceptance_min_confidence=0.6,
        )
        coordinator_agent = SimpleNamespace(
            name="CoordinatorAgent",
            root_task=lambda current: AgentTask(id="task:root", title="root"),
            remember_acceptance=lambda artifact_id, reason: None,
        )

        started = time.perf_counter()
        result = await EventDrivenCoordinator(
            AgentRegistry(agents), coordinator_agent, settings
        ).run(board)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.22)
        self.assertEqual(len({id(agent.round_boards[0]) for agent in agents}), 1)
        self.assertEqual(
            [artifact.kind for artifact in result.artifacts[:3]],
            ["safety_event", "risk", "intent"],
        )

    async def test_safety_override_keeps_high_risk_when_low_artifact_merges_later(self):
        board = (
            CollaborationBlackboard(turn_id="turn")
            .append_event(AgentEvent(type=AgentEventType.SAFETY_OVERRIDE, actor="Safety"))
            .add_artifact(
                AgentArtifact(
                    id="low",
                    owner="Risk",
                    kind="risk",
                    payload={"risk": "LOW"},
                )
            )
        )

        from app.agents.coordinator import _risk_value

        self.assertEqual(_risk_value(board).value, "HIGH")

    async def test_one_agent_failure_publishes_fallback_without_cancelling_peers(self):
        failed = FailingAgent("Broken", "broken", 1, ValueError("永久失败"))
        healthy = SleepingAgent("Healthy", "healthy")
        board = CollaborationBlackboard(
            turn_id="turn",
            tasks={
                "task:Broken": AgentTask(id="task:Broken", title="Broken", metadata={"kind": "demo"}),
                "task:Healthy": AgentTask(id="task:Healthy", title="Healthy", metadata={"kind": "demo"}),
            },
        )
        settings = SimpleNamespace(
            agent_max_rounds=1,
            agent_max_claims_per_round=2,
            agent_max_claims_per_agent=1,
            agent_final_acceptance_min_confidence=0.6,
            agent_task_max_attempts=2,
            agent_task_timeout_seconds=1,
            agent_retry_base_seconds=0,
        )
        coordinator_agent = SimpleNamespace(
            name="CoordinatorAgent",
            root_task=lambda current: AgentTask(id="task:root", title="root"),
            remember_acceptance=lambda artifact_id, reason: None,
        )

        result = await EventDrivenCoordinator(AgentRegistry([failed, healthy]), coordinator_agent, settings).run(board)

        self.assertIsNotNone(result.latest_artifact("healthy"))
        self.assertIsNotNone(result.latest_artifact("agent_failure"))
        self.assertEqual(result.tasks["task:Broken"].status.value, "FAILED")
        self.assertTrue(any(event.type == AgentEventType.TASK_FAILED for event in result.events))

    async def test_transient_local_model_failure_retries_then_succeeds(self):
        flaky = FailingAgent("Flaky", "intent", 1, httpx.ConnectError("ollama connection refused"))
        board = CollaborationBlackboard(
            turn_id="turn",
            tasks={"task:Flaky": AgentTask(id="task:Flaky", title="Flaky", max_attempts=2)},
        )
        settings = SimpleNamespace(
            agent_max_rounds=1,
            agent_max_claims_per_round=1,
            agent_max_claims_per_agent=1,
            agent_final_acceptance_min_confidence=0.6,
            agent_task_max_attempts=2,
            agent_task_timeout_seconds=1,
            agent_retry_base_seconds=0,
        )
        coordinator_agent = SimpleNamespace(
            name="CoordinatorAgent",
            root_task=lambda current: AgentTask(id="task:root", title="root"),
            remember_acceptance=lambda artifact_id, reason: None,
        )

        result = await EventDrivenCoordinator(AgentRegistry([flaky]), coordinator_agent, settings).run(board)

        self.assertEqual(flaky.calls, 2)
        self.assertIsNotNone(result.latest_artifact("intent"))
        self.assertTrue(any(event.type == AgentEventType.TASK_RETRY_SCHEDULED for event in result.events))

    async def test_context_overflow_retries_with_reactive_board_then_succeeds(self):
        overflow = ModelError(
            code=ModelErrorCode.PROMPT_TOO_LONG,
            message="provider rejected oversized prompt",
            retryable=True,
            provider="ollama",
            model="local-model",
        )
        flaky = FailingAgent("Flaky", "intent", 1, overflow)
        board = CollaborationBlackboard(
            turn_id="turn",
            tasks={"task:Flaky": AgentTask(id="task:Flaky", title="Flaky", max_attempts=3)},
        ).add_artifact(
            AgentArtifact(
                id="context",
                owner="ContextAgent",
                kind="context",
                payload={"modelHistory": ["evidence"]},
            )
        )
        settings = SimpleNamespace(
            agent_max_rounds=1,
            agent_max_claims_per_round=1,
            agent_max_claims_per_agent=1,
            agent_final_acceptance_min_confidence=0.6,
            agent_task_max_attempts=3,
            agent_task_timeout_seconds=1,
            agent_retry_base_seconds=0,
        )
        coordinator_agent = SimpleNamespace(
            name="CoordinatorAgent",
            root_task=lambda current: AgentTask(id="task:root", title="root"),
            remember_acceptance=lambda artifact_id, reason: None,
        )

        result = await EventDrivenCoordinator(
            AgentRegistry([flaky]), coordinator_agent, settings
        ).run(board)

        self.assertEqual(flaky.calls, 2)
        retry_context = flaky.round_boards[0].latest_artifact("context")
        self.assertEqual(retry_context.payload["modelHistory"], ["evidence"])
        self.assertTrue(retry_context.metadata["reactiveCompacted"])
        self.assertIsNotNone(result.latest_artifact("intent"))

    async def test_second_context_overflow_stops_without_retry_loop(self):
        overflow = ModelError(
            code=ModelErrorCode.PROMPT_TOO_LONG,
            message="provider rejected oversized prompt",
            retryable=True,
            provider="ollama",
            model="local-model",
        )
        failing = FailingAgent("Overflow", "intent", 10, overflow)
        board = CollaborationBlackboard(
            turn_id="turn",
            tasks={
                "task:Overflow": AgentTask(
                    id="task:Overflow",
                    title="Overflow",
                    max_attempts=3,
                )
            },
        ).add_artifact(
            AgentArtifact(
                id="context",
                owner="ContextAgent",
                kind="context",
                payload={"modelHistory": ["evidence"]},
            )
        )
        settings = SimpleNamespace(
            agent_max_rounds=1,
            agent_max_claims_per_round=1,
            agent_max_claims_per_agent=1,
            agent_final_acceptance_min_confidence=0.6,
            agent_task_max_attempts=3,
            agent_task_timeout_seconds=1,
            agent_retry_base_seconds=0,
        )
        coordinator_agent = SimpleNamespace(
            name="CoordinatorAgent",
            root_task=lambda current: AgentTask(id="task:root", title="root"),
            remember_acceptance=lambda artifact_id, reason: None,
        )

        result = await EventDrivenCoordinator(
            AgentRegistry([failing]), coordinator_agent, settings
        ).run(board)

        self.assertEqual(failing.calls, 2)
        self.assertEqual(result.tasks["task:Overflow"].status.value, "FAILED")
        retry_events = [
            event
            for event in result.events
            if event.type == AgentEventType.TASK_RETRY_SCHEDULED
        ]
        self.assertEqual(len(retry_events), 1)


if __name__ == "__main__":
    unittest.main()
