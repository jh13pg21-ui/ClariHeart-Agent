import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.harness import MindBridgeAgentHarness
from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, OutboxEvent, UserAccount
from app.workers.outbox_publisher import CeleryBroker


class MemoryOutboxTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.user = UserAccount(
            username="student",
            display_name="学生",
            password_hash="hash",
        )
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(
            public_id="session",
            user_id=self.user.id,
            title="会话",
        )
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _harness(self):
        harness = MindBridgeAgentHarness.__new__(MindBridgeAgentHarness)
        harness.db = self.db
        harness.settings = SimpleNamespace(long_term_memory_extract_min_new_messages=6)
        harness.memory = SimpleNamespace(append=lambda *args: None)
        return harness

    def test_assistant_message_and_extraction_event_commit_together(self):
        self.db.add(
            ChatMessage(
                user_id=self.user.id,
                session_id=self.session.id,
                role="USER",
                content="以后请叫我小林。",
            )
        )
        self.db.commit()
        self._harness().save_assistant_message(
            self.user,
            self.session,
            "好的，我会记住。",
        )

        message = self.db.query(ChatMessage).filter(ChatMessage.role == "ASSISTANT").one()
        event = self.db.query(OutboxEvent).one()
        self.assertEqual(event.event_type, "memory.extract")
        self.assertEqual(event.aggregate_type, "chat_message")
        self.assertEqual(event.aggregate_id, str(message.id))
        self.assertEqual(
            CeleryBroker.TASKS["memory.extract"][0],
            "app.workers.tasks.extract_long_term_memory",
        )

    def test_high_risk_response_can_skip_long_term_extraction(self):
        self._harness().save_assistant_message(
            self.user,
            self.session,
            "请先确保当前安全。",
            extract_long_term_memory=False,
        )

        self.assertEqual(self.db.query(ChatMessage).count(), 1)
        self.assertEqual(self.db.query(OutboxEvent).count(), 0)


if __name__ == "__main__":
    unittest.main()
