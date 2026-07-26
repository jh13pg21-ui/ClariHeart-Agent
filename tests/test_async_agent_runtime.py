import asyncio
import time
import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
