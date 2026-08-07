from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from app.agents.autonomous import (
    AgentPrivateMemory,
    AgentRuntimeServices,
    ContextAgent,
    CoordinatorAgent,
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentEvent, AgentEventType, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.agents.result import AgentRunResult, AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient, PromptTemplates
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.long_term_memory import LongTermMemoryService
from app.services.memory import RedisShortTermMemoryStore
from app.services.model_trace import SqlModelTraceSink
from app.services.output_safety import (
    OutputSafetyDecision,
    OutputSafetyStatus,
    safe_fallback,
)


class EventDrivenAgentRuntimeService:
    """Actor-style multi-agent runtime.

    Agents observe open tasks, claim work independently, and return the shared
    AgentRunResult contract consumed by the rest of the app.
    """

    framework_name = "event_driven_multi_agent"
    max_steps = 8

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        trace_sink = SqlModelTraceSink(db)
        self.ai = AiClient(settings, trace_sink=trace_sink)
        self.knowledge = KnowledgeService(db, settings)
        self.memory = RedisShortTermMemoryStore(settings)
        self.long_term_memory = LongTermMemoryService(
            db,
            settings,
            ai=self.ai,
        )
        self.model_registry = AgentModelRegistry(settings, trace_sink=trace_sink)
        self.private_memory = AgentPrivateMemory(settings)

    async def run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        model_input: str,
        response_token_sink: Callable[[str], Awaitable[None]] | None = None,
    ) -> AgentRunResult:
        services = AgentRuntimeServices(
            db=self.db,
            settings=self.settings,
            user=user,
            session=session,
            ai=self.ai,
            model_registry=self.model_registry,
            memory=self.memory,
            private_memory=self.private_memory,
            long_term_memory=self.long_term_memory,
            knowledge=self.knowledge,
            response_token_sink=response_token_sink,
        )
        coordinator_agent = CoordinatorAgent(services)
        agents = [
            UnderstandingAgent(services),
            SafetyAgent(services),
            ContextAgent(services),
            ResponseAgent(services),
        ]
        board = CollaborationBlackboard(
            turn_id=uuid.uuid4().hex,
            user_id=user.id,
            session_id=session.public_id,
            user_input=original_input,
            model_input=model_input,
        )
        board = board.append_event(
            AgentEvent(
                type=AgentEventType.TURN_STARTED,
                actor=coordinator_agent.name,
                message="user turn published to shared task board",
            )
        )
        registry = AgentRegistry(agents)
        final_board = await EventDrivenCoordinator(registry, coordinator_agent, self.settings).run(board)
        return self._to_result(final_board, user)

    def _to_result(self, board: CollaborationBlackboard, user: UserAccount) -> AgentRunResult:
        intent = self._select_intent(board)
        risk = self._select_risk(board)
        context = board.latest_artifact("context")
        risk_artifact = board.latest_artifact("risk")
        accepted = board.accepted_artifact() or board.latest_artifact("response_candidate")
        memory = board.latest_artifact("memory")
        memory_brief = "无相关历史记忆。"
        retrieved: list[SearchResult] = []
        response_text = ""
        output_safety = OutputSafetyDecision(
            OutputSafetyStatus.FALLBACK,
            safe_fallback(risk),
            "no reviewed candidate",
        )
        if memory:
            memory_brief = memory.payload.get("memoryBrief") or memory_brief
        if context:
            memory_brief = context.payload.get("memoryBrief") or memory_brief
            retrieved = context.payload.get("retrievedKnowledge") or []
        if accepted:
            review = board.latest_artifact("output_safety")
            if review and review.metadata.get("responseArtifactId") == accepted.id:
                try:
                    status = OutputSafetyStatus(review.payload.get("status"))
                except ValueError:
                    status = OutputSafetyStatus.FALLBACK
                if status in {OutputSafetyStatus.APPROVED, OutputSafetyStatus.FALLBACK}:
                    response_text = str(review.payload.get("text", "")).strip()
                    output_safety = OutputSafetyDecision(
                        status,
                        response_text or safe_fallback(risk),
                        str(review.payload.get("reason", "")),
                    )
        if not response_text:
            response_text = output_safety.text
        response_messages = [AiMessage(role="assistant", content=response_text)]
        assessment = risk_artifact.payload.get("assessment") if risk_artifact else None
        return AgentRunResult(
            intent=intent,
            risk_level=risk,
            assessment=assessment,
            retrieved_knowledge=retrieved,
            response_text=response_text,
            output_safety=output_safety,
            response_messages=response_messages,
            steps=self._events_to_steps(board),
            memory_brief=memory_brief,
            collaboration_events=list(board.events),
            collaboration_tasks=list(board.tasks.values()),
            collaboration_artifacts=list(board.artifacts),
        )

    def _select_intent(self, board: CollaborationBlackboard) -> IntentType:
        artifact = board.latest_artifact("intent")
        if not artifact:
            return IntentType.CHAT
        try:
            intent = IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
            return IntentType.CONSULT if intent == IntentType.RISK else intent
        except ValueError:
            return IntentType.CHAT

    def _select_risk(self, board: CollaborationBlackboard) -> RiskLevel:
        order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
        highest = RiskLevel.LOW
        for artifact in board.artifacts_by_kind("risk"):
            try:
                risk = RiskLevel(str(artifact.payload.get("risk", RiskLevel.LOW.value)).upper())
            except ValueError:
                risk = RiskLevel.LOW
            if order[risk] > order[highest]:
                highest = risk
        if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
            return RiskLevel.HIGH
        return highest

    def _fallback_messages(self, intent: IntentType, risk: RiskLevel, display_name: str, model_input: str) -> list[AiMessage]:
        return [
            PromptTemplates.answer_system_prompt(intent, risk, "", display_name),
            AiMessage(role="user", content=model_input),
        ]

    def _events_to_steps(self, board: CollaborationBlackboard) -> list[AgentStep]:
        steps = []
        for index, event in enumerate(board.events, start=1):
            detail = event.message or _compact_json(event.metadata)
            if event.artifact_id:
                detail = f"{detail}; artifact={event.artifact_id}" if detail else f"artifact={event.artifact_id}"
            steps.append(AgentStep(index, event.actor, event.type.value, detail))
        return steps


def _compact_json(value: Any) -> str:
    jsonable = _to_jsonable(value)
    if not jsonable:
        return ""
    return str(jsonable)[:240]


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value
