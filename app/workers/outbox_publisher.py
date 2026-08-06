from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models.entities import OutboxEvent
from app.workers.celery_app import celery_app


logger = logging.getLogger(__name__)


class CeleryBroker:
    TASKS = {
        "report.excel": ("app.workers.tasks.process_excel", "general"),
        "case.create": ("app.workers.tasks.create_case", "general"),
        "case.created": ("app.workers.tasks.send_high_risk_alert", "alert"),
        "memory.extract": (
            "app.workers.tasks.extract_long_term_memory",
            "general",
        ),
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def publish(self, event: OutboxEvent) -> None:
        task = self.TASKS.get(event.event_type)
        if task is None:
            raise ValueError(f"不支持的 Outbox 事件类型：{event.event_type}")
        task_name, queue_kind = task
        queue = (
            self.settings.celery_alert_queue
            if queue_kind == "alert"
            else self.settings.celery_general_queue
        )
        payload = json.loads(event.payload_json)
        celery_app.send_task(
            task_name,
            args=[event.event_id, int(event.aggregate_id)],
            task_id=event.event_id,
            queue=queue,
            routing_key=queue,
            headers={
                "eventType": event.event_type,
                "idempotencyKey": event.idempotency_key,
                "riskLevel": payload.get("riskLevel"),
            },
        )


class OutboxPublisher:
    def __init__(self, session_factory: sessionmaker, broker: CeleryBroker):
        self.session_factory = session_factory
        self.broker = broker

    def publish_batch(self, limit: int) -> int:
        db: Session = self.session_factory()
        published = 0
        try:
            query = (
                db.query(OutboxEvent)
                .filter(
                    OutboxEvent.status == "PENDING",
                    OutboxEvent.available_at <= datetime.utcnow(),
                )
                .order_by(OutboxEvent.created_at.asc())
                .limit(limit)
            )
            if db.get_bind().dialect.name != "sqlite":
                query = query.with_for_update(skip_locked=True)
            events = query.all()
            for event in events:
                try:
                    self.broker.publish(event)
                except Exception as exc:
                    event.attempts += 1
                    event.last_error = f"{type(exc).__name__}: {exc}"
                    if event.attempts >= max(
                        1,
                        int(
                            getattr(
                                getattr(self.broker, "settings", None),
                                "outbox_publisher_max_attempts",
                                10,
                            )
                        ),
                    ):
                        event.status = "DEAD"
                    else:
                        event.available_at = datetime.utcnow() + timedelta(
                            seconds=min(300, 2 ** event.attempts)
                        )
                    db.add(event)
                    continue
                event.status = "PUBLISHED"
                event.published_at = datetime.utcnow()
                event.last_error = ""
                db.add(event)
                published += 1
            db.commit()
            return published
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def run_forever() -> None:
    settings = get_settings()
    publisher = OutboxPublisher(SessionLocal, CeleryBroker(settings))
    while True:
        try:
            count = publisher.publish_batch(settings.outbox_publisher_batch_size)
            if count == 0:
                time.sleep(settings.outbox_publisher_poll_seconds)
        except Exception:
            logger.exception("Outbox 发布批次失败")
            time.sleep(settings.outbox_publisher_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_forever()
