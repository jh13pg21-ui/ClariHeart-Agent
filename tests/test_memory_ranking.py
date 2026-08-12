import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ChatSession, UserAccount
from app.services.long_term_memory import LongTermMemoryService
from app.services.memory_ranking import MemoryRanker


def _memory(**overrides):
    values = {
        "id": 1,
        "public_id": "memory-1",
        "name": "秋招后端",
        "description": "准备后端面试",
        "body": "学生在准备秋招后端岗位。",
        "status": "ACTIVE",
        "confidence": 0.7,
        "confirmation_count": 0,
        "usage_count": 0,
        "updated_at": datetime(2026, 8, 1),
        "last_confirmed_at": None,
        "expires_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MemoryRankingTests(unittest.TestCase):
    def test_confirmed_relevant_memory_outranks_stale_conflict(self):
        now = datetime(2026, 8, 8)
        confirmed = _memory(
            id=1,
            public_id="confirmed",
            confirmation_count=3,
            last_confirmed_at=now - timedelta(days=1),
            updated_at=now - timedelta(days=1),
        )
        stale = _memory(
            id=2,
            public_id="stale",
            status="SUPERSEDED",
            confidence=0.99,
            updated_at=now - timedelta(days=400),
        )

        ranked = MemoryRanker().rank([stale, confirmed], "秋招后端", now=now)

        self.assertEqual(ranked[0].memory.public_id, "confirmed")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_expired_memory_receives_hard_penalty(self):
        now = datetime(2026, 8, 8)
        active = _memory(id=1, public_id="active", expires_at=now + timedelta(days=2))
        expired = _memory(id=2, public_id="expired", confidence=1.0, expires_at=now - timedelta(seconds=1))

        ranked = MemoryRanker().rank([expired, active], "秋招后端", now=now)

        self.assertEqual(ranked[0].memory.public_id, "active")
        self.assertLess(ranked[1].score, 0)

    def test_mark_used_does_not_raise_fact_confidence(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        user = UserAccount(username="rank-user", display_name="学生", password_hash="hash")
        db.add(user)
        db.flush()
        session = ChatSession(public_id="rank-session", title="排序", user_id=user.id)
        db.add(session)
        db.commit()
        settings = SimpleNamespace(
            long_term_memory_enabled=True,
            long_term_memory_max_items=200,
            long_term_memory_relevant_items=5,
            sensitive_data_encryption_key="",
        )
        service = LongTermMemoryService(db, settings)
        memory = service.upsert(
            user.id,
            session.id,
            "CONTEXT",
            "秋招",
            "后端面试",
            "学生在准备秋招后端面试。",
        )
        memory.confidence = 0.6
        db.flush()

        service.mark_used([memory])

        self.assertEqual(memory.confidence, 0.6)
        self.assertEqual(memory.usage_count, 1)
        self.assertIsNotNone(memory.last_accessed_at)
        db.close()
        engine.dispose()
