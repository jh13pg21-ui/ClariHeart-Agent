import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import (
    ChatSession,
    LongTermMemory,
    MemoryConsolidationRun,
    MemoryDreamState,
    OutboxEvent,
    UserAccount,
)
from app.workers import tasks


class MemoryDreamSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        db = self.Session()
        user = UserAccount(username="dream-scheduler", display_name="学生", password_hash="hash")
        db.add(user)
        db.flush()
        for index in range(5):
            session = ChatSession(
                public_id=f"scheduled-session-{index}",
                title="Dream",
                user_id=user.id,
            )
            db.add(session)
            db.flush()
            db.add(
                LongTermMemory(
                    public_id=f"scheduled-memory-{index}",
                    user_id=user.id,
                    source_session_id=session.id,
                    memory_type="CONTEXT",
                    memory_key=f"context.item_{index}",
                    name=f"事项{index}",
                    description="长期事项",
                    body=f"学生长期事项{index}。",
                    content_hash=f"scheduled-hash-{index}",
                    status="ACTIVE",
                )
            )
        db.commit()
        self.user_id = user.id
        db.close()
        self.settings = SimpleNamespace(
            memory_consolidation_enabled=True,
            memory_consolidation_min_interval_hours=24,
            memory_consolidation_scan_interval_minutes=60,
            memory_consolidation_min_active_memories=5,
            memory_consolidation_min_modified_sessions=5,
            memory_consolidation_lease_seconds=3600,
            memory_consolidation_scan_batch_size=100,
            ai_provider="mock",
            sensitive_data_encryption_key="",
        )

    def tearDown(self):
        self.engine.dispose()

    def test_periodic_scan_reserves_once_and_emits_transactional_outbox(self):
        with (
            patch.object(tasks, "SessionLocal", self.Session),
            patch.object(tasks, "worker_settings", self.settings),
        ):
            first = tasks.scan_memory_dreams.run()
            second = tasks.scan_memory_dreams.run()

        db = self.Session()
        run = db.query(MemoryConsolidationRun).one()
        event = db.query(OutboxEvent).one()
        state = db.query(MemoryDreamState).filter_by(user_id=self.user_id).one()
        self.assertEqual(first["scheduled"], 1)
        self.assertEqual(second["scheduled"], 0)
        self.assertEqual(run.status, "QUEUED")
        self.assertEqual(run.trigger_reason, "periodic_scan")
        self.assertEqual(event.event_type, "memory.consolidate")
        self.assertEqual(event.idempotency_key, f"memory.consolidate:{run.public_id}")
        self.assertEqual(state.lease_owner, run.public_id)
        db.close()

    def test_celery_beat_and_route_include_memory_dream_scan(self):
        from app.workers.celery_app import celery_app

        schedule = celery_app.conf.beat_schedule["scan-memory-dreams"]
        self.assertEqual(schedule["task"], "app.workers.tasks.scan_memory_dreams")
        route = celery_app.conf.task_routes["app.workers.tasks.scan_memory_dreams"]
        self.assertEqual(route["queue"], "mindbridge.general")
