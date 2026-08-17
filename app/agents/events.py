from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    REVISION_REQUESTED = "REVISION_REQUESTED"
    SAFETY_OVERRIDE = "SAFETY_OVERRIDE"
    FINAL_ACCEPTED = "FINAL_ACCEPTED"
    FALLBACK_APPLIED = "FALLBACK_APPLIED"
    CLOUD_EGRESS_BLOCKED = "CLOUD_EGRESS_BLOCKED"
    CONTEXT_COMPACTED = "CONTEXT_COMPACTED"
    CONTEXT_UNRECOVERABLE = "CONTEXT_UNRECOVERABLE"


@dataclass(frozen=True)
class AgentArtifact:
    id: str
    owner: str
    kind: str
    payload: dict[str, Any]
    confidence: float = 1.0
    node_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    actor: str
    node_name: str = ""
    artifact_id: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentNodeResult:
    artifacts: tuple[AgentArtifact, ...] = field(default_factory=tuple)
    events: tuple[AgentEvent, ...] = field(default_factory=tuple)
