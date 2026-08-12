import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import OutboxEvent
from app.services.outbox import OutboxService


class OutboxServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def test_event_rolls_back_with_business_transaction(self):
        db = self.Session()
        with self.assertRaises(RuntimeError):
            with db.begin():
                OutboxService.add_event(
                    db,
                    "report.excel",
                    "report",
                    "42",
                    {"reportId": 42, "riskLevel": "LOW", "ignored": "secret"},
                    "report.excel:42",
                )
                raise RuntimeError("rollback")

        self.assertEqual(db.query(OutboxEvent).count(), 0)
        db.close()

    def test_payload_is_minimal_and_idempotency_key_is_unique(self):
        db = self.Session()
        with db.begin():
            event = OutboxService.add_event(
                db,
                "case.create",
                "report",
                "7",
                {"reportId": 7, "riskLevel": "HIGH", "other": "not-published"},
                "case.create:7",
            )

        self.assertTrue(event.event_id)
        self.assertEqual(event.status, "PENDING")
        self.assertEqual(
            set(json.loads(event.payload_json)),
            {"reportId", "riskLevel", "eventId"},
        )
        with db.begin():
            duplicate = OutboxService.add_event(
                db,
                "case.create",
                "report",
                "7",
                {"reportId": 7, "riskLevel": "HIGH"},
                "case.create:7",
            )
        self.assertEqual(duplicate.id, event.id)
        self.assertEqual(db.query(OutboxEvent).count(), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
