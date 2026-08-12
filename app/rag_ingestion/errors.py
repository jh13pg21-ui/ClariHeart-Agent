from __future__ import annotations

import re


class IngestionError(RuntimeError):
    default_code = "INGESTION_ERROR"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code or self.default_code


class RetryableIngestionError(IngestionError):
    retryable = True


class NeedsReviewError(IngestionError):
    default_code = "NEEDS_REVIEW"


class VisionSchemaInvalid(IngestionError):
    default_code = "VISION_SCHEMA_INVALID"


class CloudVisionForbidden(NeedsReviewError):
    default_code = "CLOUD_VISION_FORBIDDEN"


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s;,]+"),
    re.compile(r"(?i)(?:api[_ -]?key)\s*[:=]\s*[^\s;,]+"),
)


def sanitize_error(exc: Exception) -> tuple[str, str, bool]:
    """返回可持久化的错误分类，不泄露凭据或超长上游响应。"""

    message = str(exc)
    for pattern in _SENSITIVE_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    code = str(getattr(exc, "code", type(exc).__name__.upper()))
    retryable = bool(getattr(exc, "retryable", False))
    return code, message[:1000], retryable

