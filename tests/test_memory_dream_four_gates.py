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
    MemoryDreamState,
    UserAccount,
)
from app.services.memory_consolidation import MemoryConsolidationService


class FakeAi:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages):
        return json.dumps(self.payload, ensure_ascii=False)


class MemoryDreamFourGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(username="dream-user", display_name="学生", password_hash="hash")
        self.db.add(self.user)
        self.db.flush()
        self.sessions = []
        for index in range(5):
            session = ChatSession(
                public_id=f"dream-session-{index}",
                title="Dream",
                user_id=self.user.id,
            )
            self.db.add(session)
            self.db.flush()
            self.sessions.append(session)
            self.db.add(
                LongTermMemory(
                    public_id=f"dream-memory-{index}",
                    user_id=self.user.id,
                    source_session_id=session.id,
                    memory_type="PREFERENCE",
                    memory_key="preference.study_style",
                    name=f"学习偏好{index}",
                    description=f"偏好证据{index}",
                    body=f"学生偏好学习方式{index}。",
                    content_hash=f"dream-hash-{index}",
                    status="ACTIVE",
                    evidence_message_ids_json=json.dumps([index + 1]),
                    confidence=0.7 + index * 0.01,
                    version=index + 1,
                )
            )
        self.db.commit()
        self.now = datetime(2026, 8, 8, 12, 0, 0)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _settings(self, **overrides):
        values = {
            "memory_consolidation_enabled": True,
            "memory_consolidation_min_interval_hours": 24,
            "memory_consolidation_scan_interval_minutes": 60,
            "memory_consolidation_min_active_memories": 5,
            "memory_consolidation_min_modified_sessions": 5,
            "memory_consolidation_lease_seconds": 3600,
            "ai_provider": "mock",
            "sensitive_data_encryption_key": "",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _state(self, **values):
        state = MemoryDreamState(user_id=self.user.id, **values)
        self.db.add(state)
        self.db.commit()
        return state

    def test_each_claude_code_gate_can_block_dream(self):
        service = MemoryConsolidationService(self.db, self._settings())
        self.assertTrue(service.should_schedule(self.user.id, now=self.now))

        state = self._state(last_consolidated_at=self.now - timedelta(hours=1))
        self.assertFalse(service.should_schedule(self.user.id, now=self.now))

        state.last_consolidated_at = self.now - timedelta(days=2)
        state.last_scanned_at = self.now - timedelta(minutes=30)
        self.db.commit()
        self.assertFalse(service.should_schedule(self.user.id, now=self.now))

        state.last_scanned_at = self.now - timedelta(hours=2)
        self.db.query(LongTermMemory).filter(
            LongTermMemory.public_id.in_(("dream-memory-3", "dream-memory-4"))
        ).update({LongTermMemory.status: "SUPERSEDED"}, synchronize_session=False)
        self.db.commit()
        self.assertFalse(service.should_schedule(self.user.id, now=self.now))

        self.db.query(LongTermMemory).update(
            {LongTermMemory.status: "ACTIVE"}, synchronize_session=False
        )
        state.lease_owner = "live-owner"
        state.lease_expires_at = self.now + timedelta(minutes=30)
        self.db.commit()
        self.assertFalse(service.should_schedule(self.user.id, now=self.now))

    def test_expired_lease_is_recovered_and_replaced_atomically(self):
        old_run = MemoryConsolidationRun(
            public_id="stale-run",
            user_id=self.user.id,
            status="RUNNING",
            created_at=self.now - timedelta(hours=2),
        )
        self.db.add(old_run)
        self._state(
            last_scanned_at=self.now - timedelta(hours=2),
            last_consolidated_at=self.now - timedelta(days=2),
            lease_owner=old_run.public_id,
            lease_acquired_at=self.now - timedelta(hours=2),
            lease_expires_at=self.now - timedelta(seconds=1),
        )

        run = MemoryConsolidationService(
            self.db,
            self._settings(),
        ).reserve_schedule(self.user.id, now=self.now, trigger_reason="periodic_scan")
        self.db.commit()

        state = self.db.query(MemoryDreamState).filter_by(user_id=self.user.id).one()
        self.assertIsNotNone(run)
        self.assertEqual(old_run.status, "FAILED")
        self.assertEqual(state.lease_owner, run.public_id)
        self.assertEqual(state.lease_expires_at, self.now + timedelta(hours=1))
        self.assertEqual(run.trigger_reason, "periodic_scan")

    async def test_merge_creates_new_memory_with_evidence_union_and_lineage(self):
        payload = {
            "decisions": [
                {
                    "action": "MERGE",
                    "sourceMemoryIds": ["dream-memory-0", "dream-memory-1"],
                    "mergedMemory": {
                        "type": "PREFERENCE",
                        "memoryKey": "preference.study_style",
                        "name": "学习方式偏好",
                        "description": "学生具有稳定的学习方式偏好",
                        "body": "学生偏好结构化且循序渐进的学习方式。",
                        "reason": "两条记忆描述同一语义槽位",
                    },
                },
                {"action": "KEEP", "sourceMemoryIds": ["dream-memory-2"]},
            ]
        }
        result = await MemoryConsolidationService(
            self.db,
            self._settings(),
            ai=FakeAi(payload),
        ).consolidate(self.user.id)

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.merged, 1)
        self.assertEqual(self.db.query(LongTermMemory).count(), 6)
        merged = (
            self.db.query(LongTermMemory)
            .filter(LongTermMemory.extraction_method == "dream")
            .one()
        )
        self.assertEqual(merged.status, "ACTIVE")
        self.assertEqual(json.loads(merged.evidence_message_ids_json), [1, 2])
        self.assertEqual(
            json.loads(merged.consolidated_from_ids_json),
            ["dream-memory-0", "dream-memory-1"],
        )
        self.assertEqual(self.db.get(LongTermMemory, self.memories[0].id).status, "SUPERSEDED")
        self.assertEqual(self.db.get(LongTermMemory, self.memories[1].id).status, "SUPERSEDED")

    async def test_late_invalid_merge_causes_zero_partial_memory_mutation(self):
        payload = {
            "decisions": [
                {
                    "action": "MERGE",
                    "sourceMemoryIds": ["dream-memory-0", "dream-memory-1"],
                    "mergedMemory": {
                        "type": "PREFERENCE",
                        "memoryKey": "preference.study_style",
                        "name": "学习方式偏好",
                        "description": "有效合并",
                        "body": "学生偏好结构化学习。",
                        "reason": "同一槽位",
                    },
                },
                {
                    "action": "MERGE",
                    "sourceMemoryIds": ["dream-memory-2", "dream-memory-3"],
                    "mergedMemory": {
                        "type": "INVENTED",
                        "memoryKey": "context.invalid",
                        "name": "非法类型",
                        "description": "应整批拒绝",
                        "body": "这条合并不应写入。",
                        "reason": "非法",
                    },
                },
            ]
        }
        result = await MemoryConsolidationService(
            self.db,
            self._settings(),
            ai=FakeAi(payload),
        ).consolidate(self.user.id)

        self.assertEqual(result.status, "INVALID_OUTPUT")
        self.assertEqual(self.db.query(LongTermMemory).count(), 5)
        self.assertTrue(all(item.status == "ACTIVE" for item in self.memories))

    @property
    def memories(self):
        return (
            self.db.query(LongTermMemory)
            .filter(LongTermMemory.public_id.like("dream-memory-%"))
            .order_by(LongTermMemory.id)
            .all()
        )
