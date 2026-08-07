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


class LongTermMemoryV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(
            username="student-v2",
            display_name="学生",
            password_hash="hash",
        )
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(
            public_id="memory-v2",
            title="记忆测试",
            user_id=self.user.id,
        )
        self.db.add(self.session)
        self.db.flush()
        self.user_message = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="USER",
            content="我希望以后先给结论。",
        )
        self.assistant_message = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="ASSISTANT",
            content="好的。",
        )
        self.db.add_all([self.user_message, self.assistant_message])
        self.db.commit()
        self.settings = SimpleNamespace(
            long_term_memory_enabled=True,
            long_term_memory_max_items=200,
            long_term_memory_relevant_items=5,
            long_term_memory_extract_messages=10,
            ai_provider="ollama",
            ollama_model="memory-model",
            sensitive_data_encryption_key="",
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_new_memory_without_valid_user_evidence_is_rejected(self):
        service = LongTermMemoryService(self.db, self.settings)
        missing = service.upsert_candidate(
            self.user.id,
            self.session.id,
            MemoryCandidate(
                "PREFERENCE",
                "回复风格",
                "简洁",
                "学生希望先给结论。",
                (),
                0.9,
            ),
        )
        assistant_only = service.upsert_candidate(
            self.user.id,
            self.session.id,
            MemoryCandidate(
                "PREFERENCE",
                "回复风格",
                "简洁",
                "学生希望先给结论。",
                (self.assistant_message.id,),
                0.9,
            ),
        )

        self.assertIsNone(missing)
        self.assertIsNone(assistant_only)
        self.assertEqual(self.db.query(LongTermMemory).count(), 0)

    async def test_model_extraction_records_evidence_and_provenance(self):
        payload = [
            {
                "type": "PREFERENCE",
                "name": "回复风格",
                "description": "先给结论",
                "body": "学生希望以后先给结论。",
                "confidence": 0.92,
                "evidenceMessageIds": [self.user_message.id],
            }
        ]
        service = LongTermMemoryService(
            self.db,
            self.settings,
            ai=FakeAi(payload),
        )

        stored = await service.extract_from_messages(
            self.user.id,
            self.session.id,
            [
                (self.user_message.id, "USER", self.user_message.content),
                (
                    self.assistant_message.id,
                    "ASSISTANT",
                    self.assistant_message.content,
                ),
            ],
        )

        self.assertEqual(len(stored), 1)
        memory = stored[0]
        self.assertEqual(json.loads(memory.evidence_message_ids_json), [self.user_message.id])
        self.assertEqual(memory.prompt_version, "memory_candidate_v2")
        self.assertEqual(memory.model_provider, "ollama")
        self.assertEqual(memory.model_name, "memory-model")
        self.assertEqual(memory.extraction_method, "model")
        self.assertAlmostEqual(memory.confidence, 0.92)

    def test_changed_content_creates_version_and_supersedes_old_row(self):
        service = LongTermMemoryService(self.db, self.settings)
        first = service.upsert_candidate(
            self.user.id,
            self.session.id,
            MemoryCandidate(
                "PREFERENCE",
                "回复风格",
                "先给结论",
                "学生希望先给结论。",
                (self.user_message.id,),
                0.8,
            ),
        )
        second_user = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="USER",
            content="现在希望回答更详细。",
        )
        self.db.add(second_user)
        self.db.flush()
        second = service.upsert_candidate(
            self.user.id,
            self.session.id,
            MemoryCandidate(
                "PREFERENCE",
                "回复风格",
                "更详细",
                "学生现在希望回答更详细。",
                (second_user.id,),
                0.9,
            ),
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.status, "SUPERSEDED")
        self.assertEqual(second.status, "ACTIVE")
        self.assertEqual(second.supersedes_memory_id, first.id)
        self.assertEqual(second.version, 2)
        self.assertIn("先给结论", service.protector.reveal(first.body))

    def test_exact_content_appends_evidence_instead_of_creating_row(self):
        service = LongTermMemoryService(self.db, self.settings)
        candidate = MemoryCandidate(
            "PREFERENCE",
            "回复风格",
            "先给结论",
            "学生希望先给结论。",
            (self.user_message.id,),
            0.8,
        )
        first = service.upsert_candidate(
            self.user.id,
            self.session.id,
            candidate,
        )
        again = service.upsert_candidate(
            self.user.id,
            self.session.id,
            candidate,
        )

        self.assertEqual(first.id, again.id)
        self.assertEqual(self.db.query(LongTermMemory).count(), 1)
        self.assertEqual(again.confirmation_count, 1)
