from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum

import httpx


class ModelErrorCode(str, Enum):
    RATE_LIMITED = "RATE_LIMITED"
    OVERLOADED = "OVERLOADED"
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    AUTHENTICATION = "AUTHENTICATION"
    PROMPT_TOO_LONG = "PROMPT_TOO_LONG"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
    CONTENT_POLICY = "CONTENT_POLICY"
    CLOUD_EGRESS_DENIED = "CLOUD_EGRESS_DENIED"
    PERMANENT = "PERMANENT"


@dataclass
class ModelError(RuntimeError):
    code: ModelErrorCode
    message: str
    retryable: bool
    provider: str
    model: str
    retry_after_seconds: float | None = None
    attempt: int = 0
    status_code: int | None = None

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)


_PROMPT_TOO_LONG_MARKERS = (
    "context_length_exceeded",
    "prompt_is_too_long",
    "prompt too long",
    "maximum context window",
    "max_context_window",
    "context window reached",
    "上下文过长",
)


def classify_http_error(
    response: httpx.Response,
    provider: str,
    model: str,
) -> ModelError:
    status = int(response.status_code)
    body = _response_text(response)
    lowered = body.lower()
    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))

    if status == 413 or any(marker in lowered for marker in _PROMPT_TOO_LONG_MARKERS):
        code, retryable = ModelErrorCode.PROMPT_TOO_LONG, True
    elif status == 429:
        code, retryable = ModelErrorCode.RATE_LIMITED, True
    elif status in {502, 503, 504, 529}:
        code, retryable = ModelErrorCode.OVERLOADED, True
    elif status in {408}:
        code, retryable = ModelErrorCode.TIMEOUT, True
    elif status in {401, 403}:
        code, retryable = ModelErrorCode.AUTHENTICATION, False
    elif status in {400, 404, 405, 409, 422}:
        code, retryable = ModelErrorCode.PERMANENT, False
    elif status >= 500:
        code, retryable = ModelErrorCode.OVERLOADED, True
    else:
        code, retryable = ModelErrorCode.PERMANENT, False

    return ModelError(
        code=code,
        message=_bounded_message(body or f"HTTP {status}"),
        retryable=retryable,
        provider=provider,
        model=model,
        retry_after_seconds=retry_after,
        status_code=status,
    )


def classify_transport_error(
    exc: BaseException,
    provider: str,
    model: str,
) -> ModelError:
    if isinstance(exc, ModelError):
        return exc
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        code, retryable = ModelErrorCode.TIMEOUT, True
    elif isinstance(exc, httpx.NetworkError):
        code, retryable = ModelErrorCode.NETWORK, True
    else:
        code, retryable = ModelErrorCode.PERMANENT, False
    return ModelError(
        code=code,
        message=_bounded_message(f"{type(exc).__name__}: {exc}"),
        retryable=retryable,
        provider=provider,
        model=model,
    )


def _response_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:
        return f"HTTP {response.status_code}"


def _bounded_message(message: str, limit: int = 500) -> str:
    return " ".join(str(message or "").split())[:limit]


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

