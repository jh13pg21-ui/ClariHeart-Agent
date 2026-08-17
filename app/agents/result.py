from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from app.services.output_safety import OutputSafetyDecision


@dataclass
class AgentStep:
    step: int
    agent: str
    action: str
    observation: str


@dataclass
class AgentRunResult:
    intent: IntentType
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    retrieved_knowledge: list[SearchResult]
    response_text: str
    output_safety: OutputSafetyDecision
    response_messages: list[AiMessage]
    steps: list[AgentStep]
    memory_brief: str
    collaboration_events: list[Any] = field(default_factory=list)
    collaboration_artifacts: list[Any] = field(default_factory=list)
    turn_id: str = ""
    runtime_name: str = "langgraph"
    checkpoint_resumed: bool = False

    @property
    def requires_report(self) -> bool:
        return self.intent != IntentType.CHAT or self.risk_level != RiskLevel.LOW
