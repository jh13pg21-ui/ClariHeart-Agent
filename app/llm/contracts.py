from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.enums import RiskLevel
from app.schemas.dtos import AiMessage


@dataclass(frozen=True)
class ModelRequest:
    request_id: str
    agent_name: str
    task_name: str
    risk_level: RiskLevel
    messages: tuple[AiMessage, ...]
    preferred_provider: str
    preferred_model: str
    max_output_tokens: int
    stream: bool = False
    output_schema_id: str = ""
    prompt_manifest_hash: str = ""
    cloud_egress_allowed: bool = False
    sanitized: bool = False
    context_section_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelResult:
    text: str
    provider: str
    model: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    request_id: str
    provider_request_id: str = ""
    partial: bool = False
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class ModelStreamEvent:
    kind: str
    request_id: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    provider_request_id: str = ""

