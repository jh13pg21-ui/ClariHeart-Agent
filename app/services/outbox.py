from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import OutboxEvent


class OutboxService:
    """在调用方的业务事务中登记待发布事件。"""

    @staticmethod
    def add_event(
        db: Session,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str | int,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> OutboxEvent:
        existing = (
            db.query(OutboxEvent)
            .filter(OutboxEvent.idempotency_key == idempotency_key)
            .first()
        )
        if existing is not None:
            return existing

        event_id = uuid.uuid4().hex
        minimal_payload = {
            "reportId": payload.get("reportId"),
            "riskLevel": payload.get("riskLevel"),
            "eventId": event_id,
        }
        if payload.get("jobId") is not None:
            minimal_payload["jobId"] = payload["jobId"]
        if payload.get("consolidationRunId") is not None:
            minimal_payload["consolidationRunId"] = payload[
                "consolidationRunId"
            ]
        event = OutboxEvent(
            event_id=event_id,
            idempotency_key=idempotency_key,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload_json=json.dumps(minimal_payload, ensure_ascii=False, separators=(",", ":")),
            status="PENDING",
            attempts=0,
            last_error="",
        )
        db.add(event)
        db.flush()
        return event
