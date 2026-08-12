from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from enum import Enum

import httpx

from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.llm.errors import ModelError, ModelErrorCode


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
    if isinstance(exc, ModelError):
        if exc.code == ModelErrorCode.PROMPT_TOO_LONG:
            return AgentExecutionFailure(
                AgentFailureKind.CONTEXT_OVERFLOW,
                error_type,
                message,
                exc.retryable,
            )
        if exc.code == ModelErrorCode.TIMEOUT:
            return AgentExecutionFailure(
                AgentFailureKind.TIMEOUT,
                error_type,
                message,
                exc.retryable,
            )
        if exc.code in {
            ModelErrorCode.RATE_LIMITED,
            ModelErrorCode.OVERLOADED,
            ModelErrorCode.NETWORK,
            ModelErrorCode.STREAM_INTERRUPTED,
        }:
            return AgentExecutionFailure(
                AgentFailureKind.TRANSIENT,
                error_type,
                message,
                exc.retryable,
            )
        return AgentExecutionFailure(
            AgentFailureKind.PERMANENT,
            error_type,
            message,
            False,
        )
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
    """标记一次响应边界的应急规划，不在黑板层截断或改写证据。"""

    artifacts = []
    for artifact in board.artifacts:
        metadata = dict(artifact.metadata)
        if artifact.kind in {"memory", "context"}:
            metadata.update(
                {
                    "reactiveCompacted": True,
                    "contextRecoveryAttempt": int(
                        metadata.get("contextRecoveryAttempt", 0)
                    )
                    + 1,
                }
            )
        artifacts.append(
            AgentArtifact(
                id=artifact.id,
                owner=artifact.owner,
                kind=artifact.kind,
                payload=artifact.payload,
                confidence=artifact.confidence,
                task_id=artifact.task_id,
                metadata=metadata,
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


def has_reactive_context_retry(board: CollaborationBlackboard) -> bool:
    return any(
        artifact.kind in {"memory", "context"}
        and bool(artifact.metadata.get("reactiveCompacted"))
        for artifact in board.artifacts
    )
