"""只接受元数据白名单的模型调用追踪。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from sqlalchemy.orm import Session

from app.models.entities import ModelCallTrace


@dataclass(frozen=True)
class ModelTraceEvent:
    kind: str
    request_id: str
    user_id: int | None = None
    session_id: int | None = None
    agent_name: str = ""
    task_name: str = ""
    provider: str = ""
    model: str = ""
    risk_level: str = "LOW"
    route: str = ""
    error_code: str = ""
    prompt_release: str = ""
    prompt_manifest_hash: str = ""
    context_plan_hash: str = ""
    context_section_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    attempt: int = 1
    retry_count: int = 0
    cloud_egress: bool = False
    prompt: str = ""
    messages: Any = None


class ModelTraceSink(Protocol):
    def record(self, event: ModelTraceEvent | Mapping[str, Any]) -> None: ...


class NullModelTraceSink:
    def record(self, event: ModelTraceEvent | Mapping[str, Any]) -> None:
        return None


class SqlModelTraceSink:
    """数据库实现使用正向字段白名单，未知字段永远不会落库。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def record(self, event: ModelTraceEvent | Mapping[str, Any]) -> None:
        try:
            value = event if isinstance(event, Mapping) else event.__dict__
            with self.db.begin_nested():
                row = ModelCallTrace(
                    request_id=str(value.get("request_id", ""))[:64],
                    user_id=self._optional_int(value.get("user_id")),
                    session_id=self._optional_int(value.get("session_id")),
                    agent_name=str(value.get("agent_name", ""))[:128],
                    task_name=str(value.get("task_name", ""))[:128],
                    provider=str(value.get("provider", ""))[:64],
                    model=str(value.get("model", ""))[:256],
                    risk_level=str(value.get("risk_level", "LOW"))[:32],
                    route=str(value.get("route", ""))[:64],
                    status=str(value.get("kind", "unknown")).upper()[:32],
                    error_code=str(value.get("error_code", ""))[:64],
                    prompt_release=str(value.get("prompt_release", ""))[:64],
                    prompt_manifest_hash=str(
                        value.get("prompt_manifest_hash", "")
                    )[:64],
                    context_plan_hash=str(value.get("context_plan_hash", ""))[:64],
                    context_section_count=max(
                        0,
                        int(value.get("context_section_count", 0) or 0),
                    ),
                    input_tokens=max(0, int(value.get("input_tokens", 0) or 0)),
                    output_tokens=max(0, int(value.get("output_tokens", 0) or 0)),
                    latency_ms=max(0.0, float(value.get("latency_ms", 0.0) or 0.0)),
                    attempt=max(0, int(value.get("attempt", 1) or 0)),
                    retry_count=max(0, int(value.get("retry_count", 0) or 0)),
                    cloud_egress=bool(value.get("cloud_egress", False)),
                )
                self.db.add(row)
                self.db.flush()
        except Exception:
            return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
