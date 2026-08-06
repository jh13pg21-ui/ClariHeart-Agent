from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from enum import Enum

import httpx

from app.agents.events import AgentArtifact, CollaborationBlackboard


class AgentFailureKind(str, Enum):
    TIMEOUT = "TIMEOUT"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"


@dataclass(frozen=True)
class AgentExecutionFailure:
    kind: AgentFailureKind
    error_type: str
    message: str
    retryable: bool


def classify_agent_error(exc: BaseException) -> AgentExecutionFailure:
    message = str(exc or "")
    lowered = message.lower()
    error_type = type(exc).__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return AgentExecutionFailure(AgentFailureKind.TIMEOUT, error_type, message or "task timed out", True)
    if any(
        marker in lowered
        for marker in (
            "context_length_exceeded",
            "prompt_is_too_long",
            "max_context_window",
            "context window",
            "prompt too long",
            "上下文过长",
        )
    ):
        return AgentExecutionFailure(AgentFailureKind.CONTEXT_OVERFLOW, error_type, message, True)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or any(
        marker in lowered
        for marker in (
            "429",
            "502",
            "503",
            "504",
            "529",
            "overloaded",
            "connection refused",
            "connection reset",
            "server disconnected",
            "temporarily unavailable",
            "ollama is busy",
        )
    ):
        return AgentExecutionFailure(AgentFailureKind.TRANSIENT, error_type, message, True)
    return AgentExecutionFailure(AgentFailureKind.PERMANENT, error_type, message, False)


def retry_delay(attempt: int, base_seconds: float, max_seconds: float, jitter_ratio: float) -> float:
    base = min(max(0.0, base_seconds) * (2 ** max(0, attempt - 1)), max(0.0, max_seconds))
    return base + random.uniform(0.0, base * max(0.0, jitter_ratio))


def compact_board_for_retry(board: CollaborationBlackboard) -> CollaborationBlackboard:
    """为本地模型的上下文溢出提供一次激进、可审计的重试视图。"""

    artifacts = []
    for artifact in board.artifacts:
        payload = dict(artifact.payload)
        if artifact.kind == "memory":
            history = list(payload.get("history", []))
            payload["history"] = history[-4:]
            payload["memoryBrief"] = str(payload.get("memoryBrief", ""))[-500:]
        elif artifact.kind == "context":
            history = list(payload.get("modelHistory", []))
            payload["modelHistory"] = history[-4:]
            payload["retrievedKnowledge"] = list(payload.get("retrievedKnowledge", []))[:2]
            payload["skillContext"] = str(payload.get("skillContext", ""))[:2000]
            payload["longTermMemoryContext"] = str(payload.get("longTermMemoryContext", ""))[:800]
        artifacts.append(
            AgentArtifact(
                id=artifact.id,
                owner=artifact.owner,
                kind=artifact.kind,
                payload=payload,
                confidence=artifact.confidence,
                task_id=artifact.task_id,
                metadata={**artifact.metadata, "reactiveCompacted": True},
            )
        )
    return CollaborationBlackboard(
        turn_id=board.turn_id,
        user_id=board.user_id,
        session_id=board.session_id,
        user_input=board.user_input,
        model_input=board.model_input,
        tasks=dict(board.tasks),
        messages=board.messages,
        artifacts=tuple(artifacts),
        events=board.events,
        final_artifact_id=board.final_artifact_id,
    )
