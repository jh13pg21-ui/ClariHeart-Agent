from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models.entities import (
    ChatMessage,
    ConversationMemorySummary,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    OutboxEvent,
)
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
        "memory.summary.refresh": (
            "app.workers.tasks.refresh_conversation_summary",
            "general",
        ),
        "knowledge.ingest": (
            "app.workers.ingestion_tasks.ingest_knowledge_document",
            "ingestion",
        ),
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def publish(self, event: OutboxEvent) -> None:
        task = self.TASKS.get(event.event_type)
        if task is None:
            raise ValueError(f"不支持的 Outbox 事件类型：{event.event_type}")
        task_name, queue_kind = task
        if queue_kind == "alert":
            queue = self.settings.celery_alert_queue
        elif queue_kind == "ingestion":
            queue = self.settings.rag_ingestion_queue
        else:
            queue = self.settings.celery_general_queue
        payload = json.loads(event.payload_json)
        args = (
            [str(payload["jobId"])]
            if event.event_type == "knowledge.ingest"
            else [event.event_id, int(event.aggregate_id)]
        )
        celery_app.send_task(
            task_name,
            args=args,
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
                        self._mark_terminal_publish_failure(db, event)
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

    @staticmethod
    def _mark_terminal_publish_failure(db: Session, event: OutboxEvent) -> None:
        if event.event_type == "memory.summary.refresh":
            message = db.get(ChatMessage, int(event.aggregate_id))
            if message is None:
                return
            record = (
                db.query(ConversationMemorySummary)
                .filter(ConversationMemorySummary.session_id == message.session_id)
                .first()
            )
            if record is not None and int(
                record.scheduled_through_message_id or 0
            ) <= int(message.id):
                record.scheduled_through_message_id = None
                record.scheduled_at = None
                record.last_error = "摘要刷新事件投递失败，已释放调度水位线"
                record.updated_at = datetime.utcnow()
                db.add(record)
            return
        if event.event_type != "knowledge.ingest":
            return
        job = db.get(KnowledgeIngestionJob, event.aggregate_id)
        if job is None or job.status != "PENDING":
            return
        job.status = "FAILED"
        job.stage = "QUEUE_FAILED"
        job.error_code = "QUEUE_PUBLISH_FAILED"
        job.error_message = "入库任务多次投递失败，请检查 RabbitMQ 后重试。"
        job.error_retryable = True
        job.finished_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        version = db.get(KnowledgeDocumentVersion, job.document_version_id)
        if version is not None:
            version.status = "FAILED"


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
