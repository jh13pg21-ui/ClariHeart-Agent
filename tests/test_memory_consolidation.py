import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import (
    ChatSession,
    LongTermMemory,
    MemoryConsolidationRun,
    UserAccount,
)
from app.services.memory_consolidation import MemoryConsolidationService


class FakeAi:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages):
        return json.dumps(self.payload, ensure_ascii=False)


class MemoryConsolidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(
            username="consolidation-user",
            display_name="学生",
            password_hash="hash",
        )
        self.db.add(self.user)
        self.db.flush()
        self.sessions = [
            ChatSession(
                public_id=f"consolidation-{index}",
                title="整合",
                user_id=self.user.id,
            )
            for index in range(2)
        ]
        self.db.add_all(self.sessions)
        self.db.flush()
        self.memories = [self._memory(index) for index in range(3)]
        self.db.add_all(self.memories)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _settings(self, **overrides):
        values = {
            "memory_consolidation_enabled": True,
            "memory_consolidation_min_interval_hours": 24,
            "memory_consolidation_min_active_memories": 2,
            "memory_consolidation_min_modified_sessions": 2,
            "ai_provider": "mock",
            "sensitive_data_encryption_key": "",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _memory(self, index):
        return LongTermMemory(
            public_id=f"memory-{index}",
            user_id=self.user.id,
            source_session_id=self.sessions[index % 2].id,
            memory_type="CONTEXT",
            name=f"事项{index}",
            description=f"描述{index}",
            body=f"学生长期事项{index}",
            content_hash=f"hash-{index}",
            status="ACTIVE",
            confidence=0.8,
            extraction_method="model",
        )

    def test_each_closed_gate_blocks_scheduling(self):
        self.assertTrue(
            MemoryConsolidationService(
                self.db,
                self._settings(),
            ).should_schedule(self.user.id)
        )

        cases = {
            "disabled": self._settings(memory_consolidation_enabled=False),
            "count": self._settings(memory_consolidation_min_active_memories=10),
            "sessions": self._settings(memory_consolidation_min_modified_sessions=3),
        }
        for name, settings in cases.items():
            with self.subTest(gate=name):
                self.assertFalse(
                    MemoryConsolidationService(self.db, settings).should_schedule(
                        self.user.id
                    )
                )

        recent = MemoryConsolidationRun(
            public_id="recent-success",
            user_id=self.user.id,
            status="SUCCESS",
            finished_at=datetime.utcnow(),
        )
        self.db.add(recent)
        self.db.commit()
        self.assertFalse(
            MemoryConsolidationService(
                self.db,
                self._settings(),
            ).should_schedule(self.user.id)
        )
        recent.status = "RUNNING"
        recent.finished_at = datetime.utcnow() - timedelta(days=2)
        self.db.commit()
        self.assertFalse(
            MemoryConsolidationService(
                self.db,
                self._settings(),
            ).should_schedule(self.user.id)
        )

    async def test_consolidation_marks_rows_without_deleting(self):
        payload = {
            "decisions": [
                {
                    "action": "SUPERSEDE",
                    "sourceMemoryIds": ["memory-1", "memory-0"],
                },
                {
                    "action": "EXPIRE",
                    "sourceMemoryIds": ["memory-2"],
                },
            ]
        }
        service = MemoryConsolidationService(
            self.db,
            self._settings(),
            ai=FakeAi(payload),
        )
        original_count = self.db.query(LongTermMemory).count()

        result = await service.consolidate(self.user.id)

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.superseded, 1)
        self.assertEqual(result.expired, 1)
        self.assertEqual(self.db.query(LongTermMemory).count(), original_count)
        self.assertEqual(self.memories[0].status, "SUPERSEDED")
        self.assertEqual(self.memories[1].status, "ACTIVE")
        self.assertEqual(self.memories[2].status, "EXPIRED")

    async def test_invalid_memory_id_causes_zero_mutation(self):
        payload = {
            "decisions": [
                {
                    "action": "EXPIRE",
                    "sourceMemoryIds": ["invented-memory"],
                }
            ]
        }
        result = await MemoryConsolidationService(
            self.db,
            self._settings(),
            ai=FakeAi(payload),
        ).consolidate(self.user.id)

        self.assertEqual(result.status, "INVALID_OUTPUT")
        self.assertTrue(all(item.status == "ACTIVE" for item in self.memories))
