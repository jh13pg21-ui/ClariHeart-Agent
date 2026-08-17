from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from app.agents.events import AgentArtifact, AgentEvent, AgentEventType
from app.core.enums import EmotionLabel, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value


def artifact_to_state(artifact: AgentArtifact) -> dict[str, Any]:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "owner": artifact.owner,
        "payload": to_jsonable(artifact.payload),
        "confidence": float(artifact.confidence),
        "node_name": artifact.node_name,
        "metadata": to_jsonable(artifact.metadata),
    }


def artifact_checkpoint_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """只保留可审计头信息，禁止 Artifact 正文进入 checkpoint。"""

    metadata = dict(artifact.get("metadata") or {})
    allowed_metadata = {
        key: to_jsonable(metadata[key])
        for key in (
            "fallback",
            "responseArtifactId",
            "revisionOf",
            "reactiveCompacted",
            "contextRecoveryAttempt",
            "safetyOverride",
        )
        if key in metadata
    }
    return {
        "id": str(artifact.get("id", "")),
        "kind": str(artifact.get("kind", "")),
        "owner": str(artifact.get("owner", "")),
        "confidence": float(artifact.get("confidence", 0.0)),
        "node_name": str(artifact.get("node_name", "")),
        "metadata": allowed_metadata,
    }


def artifact_from_state(value: dict[str, Any]) -> AgentArtifact:
    kind = str(value.get("kind", ""))
    return AgentArtifact(
        id=str(value.get("id", "")),
        kind=kind,
        owner=str(value.get("owner", "LangGraph")),
        payload=_restore_payload(kind, dict(value.get("payload") or {})),
        confidence=float(value.get("confidence", 1.0)),
        node_name=str(value.get("node_name", "")),
        metadata=dict(value.get("metadata") or {}),
    )


def event_to_state(event: AgentEvent) -> dict[str, Any]:
    return {
        "type": event.type.value,
        "actor": event.actor,
        "node_name": event.node_name,
        "artifact_id": event.artifact_id,
        "message": event.message,
        "metadata": to_jsonable(event.metadata),
    }


def event_from_state(value: dict[str, Any]) -> AgentEvent:
    try:
        event_type = AgentEventType(str(value.get("type", "FALLBACK_APPLIED")))
    except ValueError:
        event_type = AgentEventType.FALLBACK_APPLIED
    return AgentEvent(
        type=event_type,
        actor=str(value.get("actor", "LangGraph")),
        node_name=str(value.get("node_name", "")),
        artifact_id=str(value.get("artifact_id", "")),
        message=str(value.get("message", "")),
        metadata=dict(value.get("metadata") or {}),
    )


def _restore_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind in {"memory", "context"}:
        for key in ("history", "modelHistory"):
            payload[key] = [
                AiMessage(**item) if isinstance(item, dict) else item
                for item in payload.get(key, [])
            ]
    if kind == "context":
        payload["retrievedKnowledge"] = [
            SearchResult(**item) if isinstance(item, dict) else item
            for item in payload.get("retrievedKnowledge", [])
        ]
    if kind == "risk" and isinstance(payload.get("assessment"), dict):
        assessment = payload["assessment"]
        payload["assessment"] = PsychologyAssessment(
            emotion=EmotionLabel(str(assessment.get("emotion", EmotionLabel.NORMAL.value))),
            emotion_score=float(assessment.get("emotion_score", 0.0)),
            risk=RiskLevel(str(assessment.get("risk", RiskLevel.LOW.value))),
            confidence=float(assessment.get("confidence", 0.0)),
            summary=str(assessment.get("summary", "")),
        )
    return payload
