from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from sqlalchemy.orm import Session

from app.agents.autonomous import AgentPrivateMemory, AgentRuntimeServices
from app.agents.result import AgentRunResult, AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.graph.artifacts import artifact_from_state, event_from_state
from app.graph.checkpoint import checkpointer_status
from app.graph.nodes import GraphRuntimeContext
from app.graph.state import initial_agent_state
from app.graph.tracing import graph_config
from app.graph.workflow import build_agent_graph
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.agent_models import AgentModelRegistry
from app.services.ai import AiClient
from app.services.knowledge import KnowledgeService
from app.services.long_term_memory import LongTermMemoryService
from app.services.memory import RedisShortTermMemoryStore
from app.services.model_trace import CompositeModelTraceSink, SqlModelTraceSink
from app.services.output_safety import OutputSafetyDecision, OutputSafetyStatus, safe_fallback
from app.services.runtime_metrics import get_runtime_metrics


class LangGraphAgentRuntime:
    """MindBridge 唯一 Agent Runtime，由 StateGraph 负责全部编排。"""

    framework_name = "langgraph"
    max_steps = 11

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        trace_sink = CompositeModelTraceSink(SqlModelTraceSink(db), get_runtime_metrics())
        self.ai = AiClient(settings, trace_sink=trace_sink)
        self.knowledge = KnowledgeService(db, settings)
        self.memory = RedisShortTermMemoryStore(settings)
        self.long_term_memory = LongTermMemoryService(db, settings, ai=self.ai)
        self.model_registry = AgentModelRegistry(settings, trace_sink=trace_sink)
        self.private_memory = AgentPrivateMemory(settings)
        get_runtime_metrics().record_runtime_selection("langgraph")

    async def run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        model_input: str,
        response_token_sink: Callable[[str], Awaitable[None]] | None = None,
        turn_id: str | None = None,
    ) -> AgentRunResult:
        del original_input
        turn_id = turn_id or uuid.uuid4().hex
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
            response_token_sink=None,
        )
        graph = build_agent_graph(self.settings)
        config = graph_config(turn_id, self.framework_name)
        graph_input = initial_agent_state(
            turn_id=turn_id,
            user_id=user.id,
            session_id=session.public_id,
            model_input=model_input,
        )
        resumed = False
        if checkpointer_status(self.settings)["enabled"]:
            snapshot = await graph.aget_state(config)
            if snapshot.next:
                graph_input = None
                resumed = True

        final_state = None
        async for part in graph.astream(
            graph_input,
            config=config,
            context=GraphRuntimeContext(
                services=services,
                node_timeout_seconds=self.settings.langgraph_node_timeout_seconds,
                node_max_attempts=max(1, self.settings.langgraph_node_max_attempts),
            ),
            stream_mode=["values", "custom"],
            version="v2",
        ):
            if part["type"] == "values":
                final_state = part["data"]
            elif part["type"] == "custom" and response_token_sink is not None:
                event = part["data"]
                if isinstance(event, dict) and event.get("kind") == "token":
                    token = str(event.get("text", ""))
                    if token:
                        await response_token_sink(token)
        if final_state is None:
            raise RuntimeError("LangGraph 未返回最终 State")
        return self._to_result(final_state, turn_id, resumed)

    def _to_result(self, state: dict, turn_id: str, resumed: bool) -> AgentRunResult:
        intent_artifact = _artifact(state, "intent")
        risk_artifact = _artifact(state, "risk")
        memory = _artifact(state, "memory")
        context = _artifact(state, "context")
        response = _artifact(state, "response_candidate")
        review = _artifact(state, "output_safety")
        intent = _intent(intent_artifact)
        risk = _risk(risk_artifact, state.get("domain_events", []))
        memory_brief = "无相关历史记忆。"
        if memory:
            memory_brief = str(memory.payload.get("memoryBrief") or memory_brief)
        retrieved = []
        if context:
            memory_brief = str(context.payload.get("memoryBrief") or memory_brief)
            retrieved = list(context.payload.get("retrievedKnowledge") or [])

        output_safety = OutputSafetyDecision(
            OutputSafetyStatus.FALLBACK,
            safe_fallback(risk),
            "no reviewed candidate",
        )
        if response and review and review.metadata.get("responseArtifactId") == response.id:
            try:
                status = OutputSafetyStatus(str(review.payload.get("status", "")))
            except ValueError:
                status = OutputSafetyStatus.FALLBACK
            if status in {OutputSafetyStatus.APPROVED, OutputSafetyStatus.FALLBACK}:
                output_safety = OutputSafetyDecision(
                    status,
                    str(review.payload.get("text", "")).strip() or safe_fallback(risk),
                    str(review.payload.get("reason", "")),
                )
        response_text = output_safety.text
        artifacts = [
            item
            for item in (memory, intent_artifact, risk_artifact, context, response, review)
            if item is not None
        ]
        steps = [
            AgentStep(
                index,
                str(item.get("agent", "LangGraph")),
                str(item.get("action", "node")),
                str(item.get("observation", "")),
            )
            for index, item in enumerate(state.get("steps", []), start=1)
        ]
        return AgentRunResult(
            intent=intent,
            risk_level=risk,
            assessment=risk_artifact.payload.get("assessment") if risk_artifact else None,
            retrieved_knowledge=retrieved,
            response_text=response_text,
            output_safety=output_safety,
            response_messages=[AiMessage(role="assistant", content=response_text)],
            steps=steps,
            memory_brief=memory_brief,
            collaboration_events=[event_from_state(item) for item in state.get("domain_events", [])],
            collaboration_artifacts=artifacts,
            turn_id=turn_id,
            runtime_name=self.framework_name,
            checkpoint_resumed=resumed,
        )


def _artifact(state: dict, key: str):
    value = state.get(key)
    return artifact_from_state(value) if value else None


def _intent(artifact) -> IntentType:
    if artifact is None:
        return IntentType.CHAT
    try:
        value = IntentType(str(artifact.payload.get("intent", "CHAT")).upper())
        return IntentType.CONSULT if value == IntentType.RISK else value
    except ValueError:
        return IntentType.CHAT


def _risk(artifact, events: list[dict]) -> RiskLevel:
    if any(item.get("type") == "SAFETY_OVERRIDE" for item in events):
        return RiskLevel.HIGH
    if artifact is None:
        return RiskLevel.LOW
    try:
        return RiskLevel(str(artifact.payload.get("risk", "LOW")).upper())
    except ValueError:
        return RiskLevel.LOW
