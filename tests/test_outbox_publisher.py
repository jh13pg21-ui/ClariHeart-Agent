import unittest
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    OutboxEvent,
)
from app.services.outbox import OutboxService
from app.workers.outbox_publisher import CeleryBroker, OutboxPublisher


class FakeBroker:
    def __init__(self, error=None, max_attempts=10):
        self.error = error
        self.events = []
        self.settings = SimpleNamespace(outbox_publisher_max_attempts=max_attempts)

    def publish(self, event):
        if self.error:
            raise self.error
        self.events.append(event.event_id)


class OutboxPublisherTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def _event(self):
        db = self.Session()
        with db.begin():
            event = OutboxService.add_event(
                db,
                "report.excel",
                "report",
                "11",
                {"reportId": 11, "riskLevel": "LOW"},
                "report.excel:11",
            )
        event_id = event.event_id
        db.close()
        return event_id

    def test_confirmed_publish_marks_event_published(self):
        event_id = self._event()
        broker = FakeBroker()

        published = OutboxPublisher(self.Session, broker).publish_batch(10)

        db = self.Session()
        event = db.query(OutboxEvent).filter_by(event_id=event_id).one()
        self.assertEqual(published, 1)
        self.assertEqual(broker.events, [event_id])
        self.assertEqual(event.status, "PUBLISHED")
        self.assertIsNotNone(event.published_at)
        db.close()

    def test_broker_error_keeps_pending_and_schedules_backoff(self):
        event_id = self._event()
        before = datetime.utcnow()

        published = OutboxPublisher(self.Session, FakeBroker(RuntimeError("down"))).publish_batch(10)

        db = self.Session()
        event = db.query(OutboxEvent).filter_by(event_id=event_id).one()
        self.assertEqual(published, 0)
        self.assertEqual(event.status, "PENDING")
        self.assertEqual(event.attempts, 1)
        self.assertGreater(event.available_at, before)
        self.assertIn("down", event.last_error)
        db.close()

    def test_broker_error_moves_poison_event_to_dead_state(self):
        event_id = self._event()

        published = OutboxPublisher(
            self.Session,
            FakeBroker(RuntimeError("broker unavailable"), max_attempts=1),
        ).publish_batch(10)

        db = self.Session()
        event = db.query(OutboxEvent).filter_by(event_id=event_id).one()
        self.assertEqual(published, 0)
        self.assertEqual(event.status, "DEAD")
        self.assertEqual(event.attempts, 1)
        self.assertIn("broker unavailable", event.last_error)
        db.close()

    def test_knowledge_ingestion_event_targets_dedicated_queue_with_job_id(self):
        settings = SimpleNamespace(
            celery_alert_queue="mindbridge.alert",
            celery_general_queue="mindbridge.general",
            rag_ingestion_queue="mindbridge.ingestion",
        )
        event = SimpleNamespace(
            event_type="knowledge.ingest",
            event_id="event-1",
            aggregate_id="job_123",
            idempotency_key="knowledge.ingest:job_123",
            payload_json=json.dumps({"jobId": "job_123"}),
        )

        with patch("app.workers.outbox_publisher.celery_app.send_task") as send_task:
            CeleryBroker(settings).publish(event)

        send_task.assert_called_once()
        args, kwargs = send_task.call_args
        self.assertEqual(args[0], "app.workers.ingestion_tasks.ingest_knowledge_document")
        self.assertEqual(kwargs["args"], ["job_123"])
        self.assertEqual(kwargs["queue"], "mindbridge.ingestion")
        self.assertEqual(kwargs["routing_key"], "mindbridge.ingestion")

    def test_exhausted_knowledge_publish_marks_job_as_retryable_failure(self):
        db = self.Session()
        document = KnowledgeDocument(
            id="doc_1",
            source_key="admin:guide.pdf",
            display_name="guide.pdf",
            mime_type="application/pdf",
            access_class="ADMIN_PRIVATE",
        )
        version = KnowledgeDocumentVersion(
            id="docver_1",
            document_id=document.id,
            sha256="a" * 64,
            size_bytes=100,
            status="PENDING",
            pipeline_fingerprint="rag-v3",
        )
        job = KnowledgeIngestionJob(
            id="job_1",
            document_version_id=version.id,
            stage="PENDING",
            status="PENDING",
            trigger_actor="admin",
        )
        db.add_all([document, version, job])
        db.flush()
        OutboxService.add_event(
            db,
            "knowledge.ingest",
            "knowledge_ingestion_job",
            job.id,
            {"jobId": job.id},
            f"knowledge.ingest:{job.id}",
        )
        db.commit()
        db.close()

        OutboxPublisher(
            self.Session,
            FakeBroker(RuntimeError("broker unavailable"), max_attempts=1),
        ).publish_batch(10)

        db = self.Session()
        failed = db.get(KnowledgeIngestionJob, "job_1")
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.stage, "QUEUE_FAILED")
        self.assertEqual(failed.error_code, "QUEUE_PUBLISH_FAILED")
        self.assertTrue(failed.error_retryable)
        self.assertEqual(db.get(KnowledgeDocumentVersion, "docver_1").status, "FAILED")
        db.close()


if __name__ == "__main__":
    unittest.main()
