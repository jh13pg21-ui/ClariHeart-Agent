import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, ConversationMemorySummary, UserAccount
from app.services.conversation_summary import ConversationSummaryService


def _settings():
    return SimpleNamespace(
        memory_compaction_enabled=True,
        memory_compaction_recent_messages=8,
        memory_summary_refresh_messages=4,
        memory_summary_max_chars=500,
        memory_summary_llm_enabled=False,
        memory_summary_llm_attempts=1,
        memory_summary_max_source_messages=40,
        memory_summary_input_max_chars=12000,
        redis_memory_max_messages=40,
        ai_provider="mock",
        sensitive_data_encryption_key="",
    )


class SummarySchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(
            username="student",
            display_name="学生",
            password_hash="hash",
        )
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(
            public_id="summary-scheduling",
            user_id=self.user.id,
            title="调度测试",
        )
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _add_messages(self, count: int) -> list[ChatMessage]:
        rows = []
        start = self.db.query(ChatMessage).count()
        for offset in range(count):
            index = start + offset
            row = ChatMessage(
                user_id=self.user.id,
                session_id=self.session.id,
                role="USER" if index % 2 == 0 else "ASSISTANT",
                content=f"消息 {index + 1}",
            )
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        return rows

    def test_second_assistant_does_not_reserve_overlapping_range(self):
        rows = self._add_messages(12)
        service = ConversationSummaryService(self.db, _settings())

        self.assertTrue(service.reserve_refresh(self.session, rows[-1]))
        first_record = self.db.query(ConversationMemorySummary).one()
        self.assertEqual(first_record.scheduled_through_message_id, rows[-1].id)

        later = self._add_messages(2)[-1]
        self.assertFalse(service.reserve_refresh(self.session, later))
        self.assertEqual(first_record.scheduled_through_message_id, rows[-1].id)

    async def test_success_advances_watermark_and_releases_reservation(self):
        rows = self._add_messages(12)
        service = ConversationSummaryService(self.db, _settings())
        self.assertTrue(service.reserve_refresh(self.session, rows[-1]))

        record = await service.ensure_through(
            self.session,
            rows[-1].id,
            reason="async_outbox",
        )

        self.assertEqual(record.through_message_id, rows[3].id)
        self.assertIsNone(record.scheduled_through_message_id)
        self.assertEqual(record.refresh_reason, "async_outbox")
        self.assertEqual(record.prompt_release, "2026.08-v1")
        self.assertGreater(record.input_tokens, 0)
        self.assertGreater(record.output_tokens, 0)

        same = await service.ensure_through(
            self.session,
            rows[-1].id,
            reason="duplicate_delivery",
        )
        self.assertEqual(same.id, record.id)
        self.assertEqual(same.through_message_id, rows[3].id)
