import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, LongTermMemory, UserAccount
from app.schemas.dtos import MemoryCandidate
from app.services.long_term_memory import LongTermMemoryService


class FakeAi:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages):
        return json.dumps(self.payload, ensure_ascii=False)


class LongTermMemoryV3ConflictTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(username="memory-v3", display_name="学生", password_hash="hash")
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(public_id="memory-v3-session", title="记忆", user_id=self.user.id)
        self.db.add(self.session)
        self.db.flush()
        self.messages = []
        for content in ("我喜欢简洁回答。", "现在请详细解释。", "还是简洁回答更适合我。"):
            row = ChatMessage(
                user_id=self.user.id,
                session_id=self.session.id,
                role="USER",
                content=content,
            )
            self.db.add(row)
            self.db.flush()
            self.messages.append(row)
        self.db.commit()
        self.settings = SimpleNamespace(
            long_term_memory_enabled=True,
            long_term_memory_max_items=200,
            long_term_memory_relevant_items=5,
            long_term_memory_extract_messages=10,
            ai_provider="mock",
            sensitive_data_encryption_key="",
        )
        self.service = LongTermMemoryService(self.db, self.settings)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _candidate(self, body, evidence, **overrides):
        values = {
            "memory_type": "PREFERENCE",
            "name": "回复风格",
            "description": body,
            "body": body,
            "evidence_message_ids": (evidence.id,),
            "confidence": 0.9,
            "memory_key": "preference.response_detail",
            "action": "CREATE",
        }
        values.update(overrides)
        return MemoryCandidate(**values)

    def test_same_memory_key_supersedes_even_when_model_changes_display_name(self):
        first = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate("学生喜欢简洁回答。", self.messages[0]),
        )
        second = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate(
                "学生现在希望得到详细解释。",
                self.messages[1],
                name="回答详细程度",
            ),
        )

        self.assertEqual(first.status, "SUPERSEDED")
        self.assertEqual(second.status, "ACTIVE")
        self.assertEqual(second.version, 2)
        self.assertEqual(second.supersedes_memory_id, first.id)
        self.assertEqual(second.memory_key, "preference.response_detail")

    async def test_explicit_conflict_is_quarantined_and_never_selected(self):
        active = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate("学生喜欢简洁回答。", self.messages[0]),
        )
        conflicted = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate(
                "学生可能希望得到详细解释。",
                self.messages[1],
                action="CONFLICT",
                related_memory_ids=(active.public_id,),
                reason="同一偏好出现矛盾且表达不明确",
            ),
        )
        self.db.commit()

        selecting = LongTermMemoryService(
            self.db,
            self.settings,
            ai=FakeAi([conflicted.public_id, active.public_id]),
        )
        selected = await selecting.select_relevant(self.user.id, "回答风格")

        self.assertEqual(conflicted.status, "CONFLICTED")
        self.assertTrue(conflicted.conflict_group_id)
        self.assertEqual(active.status, "ACTIVE")
        self.assertEqual([item.public_id for item in selected], [active.public_id])

    async def test_model_candidate_with_unknown_related_memory_id_is_rejected(self):
        payload = [
            {
                "type": "PREFERENCE",
                "memoryKey": "preference.response_detail",
                "name": "回复风格",
                "description": "详细回答",
                "body": "学生希望得到详细回答。",
                "confidence": 0.8,
                "evidenceMessageIds": [self.messages[1].id],
                "action": "SUPERSEDE",
                "relatedMemoryIds": ["invented-memory"],
                "reason": "替换旧偏好",
            }
        ]
        service = LongTermMemoryService(self.db, self.settings, ai=FakeAi(payload))

        stored = await service.extract_from_messages(
            self.user.id,
            self.session.id,
            [(self.messages[1].id, "USER", self.messages[1].content)],
        )

        self.assertEqual(stored, [])
        self.assertEqual(self.db.query(LongTermMemory).count(), 0)

    def test_repeating_superseded_fact_reactivates_it_as_latest_version(self):
        concise = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate("学生喜欢简洁回答。", self.messages[0]),
        )
        detailed = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate("学生现在希望得到详细解释。", self.messages[1]),
        )
        reverted = self.service.upsert_candidate(
            self.user.id,
            self.session.id,
            self._candidate("学生喜欢简洁回答。", self.messages[2]),
        )

        self.assertEqual(reverted.id, concise.id)
        self.assertEqual(reverted.status, "ACTIVE")
        self.assertEqual(reverted.version, 3)
        self.assertEqual(reverted.supersedes_memory_id, detailed.id)
        self.assertEqual(detailed.status, "SUPERSEDED")
        self.assertEqual(
            json.loads(reverted.evidence_message_ids_json),
            [self.messages[0].id, self.messages[2].id],
        )
