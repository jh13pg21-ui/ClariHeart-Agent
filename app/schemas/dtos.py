from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field, PlainSerializer


def _serialize_utc(value: datetime) -> str:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    sessionId: Optional[str] = None
    requestId: Optional[str] = Field(default=None, min_length=8, max_length=128)


class ChatStreamEvent(BaseModel):
    sessionId: Optional[str] = None
    requestId: Optional[str] = None
    turnId: Optional[str] = None
    eventId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    resetSession: Optional[bool] = None
    type: str


class KnowledgeIngestRequest(BaseModel):
    source: str
    content: str


class KnowledgeIngestResponse(BaseModel):
    source: str
    chunks: int


class ReportResponse(BaseModel):
    id: int
    sessionId: str
    username: str
    displayName: str
    content: str
    intent: str
    emotion: str
    emotionScore: float
    riskLevel: str
    confidence: float
    summary: str
    createdAt: UtcDateTime


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: UtcDateTime


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    messages: list[ConversationMessageResponse]


class ConversationSummaryResponse(BaseModel):
    sessionId: str
    title: str
    lastMessage: str
    createdAt: UtcDateTime
    updatedAt: UtcDateTime


class LongTermMemoryResponse(BaseModel):
    id: str
    type: str
    name: str
    description: str
    body: str
    createdAt: UtcDateTime
    updatedAt: UtcDateTime
    status: str = "ACTIVE"
    confidence: float = 0.5
    version: int = 1


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    name: str
    description: str
    body: str
    evidence_message_ids: tuple[int, ...]
    confidence: float = 0.5
    memory_key: str = ""
    action: str = "CREATE"
    related_memory_ids: tuple[str, ...] = ()
    reason: str = ""
    extraction_method: str = "model"
    prompt_version: str = "memory_candidate_v3"
    model_provider: str = ""
    model_name: str = ""


class PrivacyPreferenceRequest(BaseModel):
    longTermMemoryEnabled: bool
    purgeExistingMemories: bool = False


class PrivacyPreferenceResponse(BaseModel):
    longTermMemoryEnabled: bool
    removedMemories: int = 0


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: UtcDateTime
    channel: Optional[str] = None
    recipient: Optional[str] = None
    filePath: Optional[str] = None


class RiskCaseResponse(BaseModel):
    id: int
    reportId: int
    riskLevel: str
    status: str
    owner: str
    summary: str
    handoffSummary: str
    acknowledgedBy: Optional[str] = None
    acknowledgedAt: Optional[UtcDateTime] = None
    createdAt: UtcDateTime
    updatedAt: UtcDateTime


class CaseNoteResponse(BaseModel):
    id: int
    caseId: int
    actor: str
    note: str
    createdAt: UtcDateTime


class CaseActionRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class ToolJobResponse(BaseModel):
    id: int
    reportId: int
    kind: str
    status: str
    attempts: int
    maxAttempts: int
    dependsOnJobId: Optional[int] = None
    runAfter: UtcDateTime
    lastError: str
    createdAt: UtcDateTime
    updatedAt: UtcDateTime


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: int
    kind: str
    reason: str
    payload: str
    createdAt: UtcDateTime


class OutboxEventResponse(BaseModel):
    eventId: str
    eventType: str
    aggregateType: str
    aggregateId: str
    status: str
    attempts: int
    lastError: str
    availableAt: UtcDateTime
    createdAt: UtcDateTime


class AgentRunTraceResponse(BaseModel):
    id: int
    sessionId: str
    reportId: Optional[int] = None
    username: str
    intent: str
    riskLevel: str
    originalInput: str
    sanitizedInput: str
    memoryBrief: str
    agentSteps: list[dict[str, Any]]
    retrievedKnowledge: list[dict[str, Any]]
    responseMessages: list[dict[str, Any]]
    assessment: dict[str, Any]
    createdAt: UtcDateTime


class ToolAuditResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: Optional[int] = None
    toolName: str
    policy: str
    allowed: bool
    status: str
    reason: str
    payload: dict[str, Any]
    createdAt: UtcDateTime
    updatedAt: UtcDateTime


class AiMessage(BaseModel):
    role: str
    content: str


def authority(role: str) -> dict[str, Any]:
    return {"authority": role}
