import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import (
    LongTermMemory,
    MemoryConsolidationRun,
    ProcessedMessage,
    UserAccount,
)
from app.workers import tasks


class MemoryConsolidationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        db = self.Session()
        user = UserAccount(
            username="consolidation-worker",
            display_name="学生",
            password_hash="hash",
        )
        db.add(user)
        db.flush()
        memory = LongTermMemory(
            public_id="worker-memory",
            user_id=user.id,
            memory_type="CONTEXT",
            name="事项",
            description="描述",
            body="学生长期事项",
            content_hash="worker-hash",
            status="ACTIVE",
        )
        run = MemoryConsolidationRun(
            public_id="worker-run",
            user_id=user.id,
            status="QUEUED",
        )
        db.add_all([memory, run])
        db.commit()
        self.user_id = user.id
        db.close()
        self.settings = SimpleNamespace(
            ai_provider="mock",
            sensitive_data_encryption_key="",
        )

    def tearDown(self):
        self.engine.dispose()

    def test_duplicate_event_has_one_consolidation_side_effect(self):
        response = '{"decisions":[{"action":"EXPIRE","sourceMemoryIds":["worker-memory"]}]}'
        with (
            patch.object(tasks, "SessionLocal", self.Session),
            patch.object(tasks, "worker_settings", self.settings),
            patch(
                "app.services.memory_consolidation.registered_complete",
                new=AsyncMock(return_value=response),
            ),
        ):
            first = tasks.consolidate_long_term_memory.run(
                "event-consolidate",
                self.user_id,
                "worker-run",
            )
            second = tasks.consolidate_long_term_memory.run(
                "event-consolidate",
                self.user_id,
                "worker-run",
            )

        db = self.Session()
        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(second["status"], "ALREADY_PROCESSED")
        self.assertEqual(db.query(ProcessedMessage).count(), 1)
        self.assertEqual(db.query(LongTermMemory).one().status, "EXPIRED")
        self.assertEqual(db.query(MemoryConsolidationRun).one().status, "SUCCESS")
        db.close()
