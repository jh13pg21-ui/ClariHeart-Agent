from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

import httpx

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
        return AgentExecutionFailure(AgentFailureKind.TIMEOUT, error_type, message or "node timed out", True)
    if isinstance(exc, ModelError):
        if exc.code == ModelErrorCode.PROMPT_TOO_LONG:
            return AgentExecutionFailure(AgentFailureKind.CONTEXT_OVERFLOW, error_type, message, exc.retryable)
        if exc.code == ModelErrorCode.TIMEOUT:
            return AgentExecutionFailure(AgentFailureKind.TIMEOUT, error_type, message, exc.retryable)
        if exc.code in {
            ModelErrorCode.RATE_LIMITED,
            ModelErrorCode.OVERLOADED,
            ModelErrorCode.NETWORK,
            ModelErrorCode.STREAM_INTERRUPTED,
        }:
            return AgentExecutionFailure(AgentFailureKind.TRANSIENT, error_type, message, exc.retryable)
        return AgentExecutionFailure(AgentFailureKind.PERMANENT, error_type, message, False)
    if any(marker in lowered for marker in (
        "context_length_exceeded",
        "prompt_is_too_long",
        "max_context_window",
        "context window",
        "prompt too long",
        "上下文过长",
    )):
        return AgentExecutionFailure(AgentFailureKind.CONTEXT_OVERFLOW, error_type, message, True)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or any(
        marker in lowered
        for marker in ("429", "502", "503", "504", "529", "overloaded", "connection refused", "connection reset", "server disconnected", "temporarily unavailable", "ollama is busy")
    ):
        return AgentExecutionFailure(AgentFailureKind.TRANSIENT, error_type, message, True)
    return AgentExecutionFailure(AgentFailureKind.PERMANENT, error_type, message, False)
