import unittest
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import OutboxEvent
from app.services.outbox import OutboxService
from app.workers.outbox_publisher import OutboxPublisher


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


if __name__ == "__main__":
    unittest.main()
