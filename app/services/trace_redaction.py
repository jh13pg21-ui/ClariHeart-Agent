"""协作与模型追踪的递归正文脱敏。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


REDACTED = "[REDACTED]"
_CONTENT_KEYS = {
    "history",
    "modelhistory",
    "body",
    "content",
    "skillcontext",
    "longtermmemorycontext",
    "memorybrief",
    "summaryjson",
    "prompt",
    "messages",
    "inputtext",
    "outputtext",
}


def redact_collaboration_payload(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return redact_collaboration_payload(asdict(value))
    if hasattr(value, "model_dump"):
        return redact_collaboration_payload(value.model_dump())
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            result[str(key)] = (
                REDACTED
                if normalized in _CONTENT_KEYS
                else redact_collaboration_payload(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [redact_collaboration_payload(item) for item in value]
    return value
