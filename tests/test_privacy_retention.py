import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import AgentRunTrace, ChatMessage, ChatSession, ConversationMemorySummary, PsychologicalReport, UserAccount
from app.services.privacy_retention import PrivacyRetentionService, REDACTED_CONTENT, REDACTED_TITLE


class PrivacyRetentionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(username="student", display_name="学生", password_hash="hash")
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _session(self, suffix: str, age_days: int) -> ChatSession:
        timestamp = datetime.utcnow() - timedelta(days=age_days)
        session = ChatSession(
            public_id=f"session-{suffix}",
            title=f"会话 {suffix}",
            user_id=self.user.id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.db.add(session)
        self.db.flush()
        self.db.add(
            ChatMessage(
                user_id=self.user.id,
                session_id=session.id,
                role="USER",
                content="原始敏感正文",
                created_at=timestamp,
            )
        )
        return session

    def test_purges_expired_low_risk_but_preserves_recent_high_risk_case(self):
        old = self._session("old", 400)
        protected = self._session("protected", 400)
        old_time = datetime.utcnow() - timedelta(days=400)
        self.db.add_all(
            [
                ConversationMemorySummary(
                    session_id=old.id,
                    schema_version=1,
                    summary_json='{"studentConcerns":[]}',
                    source_message_count=2,
                    model_provider="ollama",
                    model_name="test",
                    prompt_version="structured-summary-v1",
                    status="LLM",
                    last_error="",
                    created_at=old_time,
                    updated_at=old_time,
                ),
                PsychologicalReport(
                    user_id=self.user.id,
                    session_id=old.id,
                    content="低风险正文",
                    intent="CONSULT",
                    emotion="SAD",
                    emotion_score=0.5,
                    risk_level="LOW",
                    confidence=0.8,
                    summary="低风险摘要",
                    created_at=old_time,
                ),
                PsychologicalReport(
                    user_id=self.user.id,
                    session_id=protected.id,
                    content="高风险正文",
                    intent="CONSULT",
                    emotion="SAD",
                    emotion_score=0.9,
                    risk_level="HIGH",
                    confidence=0.9,
                    summary="高风险摘要",
                    created_at=old_time,
                ),
                AgentRunTrace(
                    user_id=self.user.id,
                    session_id=old.id,
                    intent="CONSULT",
                    risk_level="LOW",
                    original_input="原文",
                    sanitized_input="脱敏原文",
                    turn_id="expired-langgraph-turn",
                    runtime_name="langgraph",
                    created_at=old_time,
                ),
            ]
        )
        self.db.commit()

        result = PrivacyRetentionService(
            self.db,
            SimpleNamespace(
                privacy_retention_enabled=True,
                chat_data_retention_days=365,
                risk_data_retention_days=1095,
            ),
        ).purge_expired()

        self.assertEqual(result.messages, 1)
        self.assertEqual(result.summaries, 1)
        self.assertEqual(result.checkpoint_thread_ids, ("expired-langgraph-turn",))
        self.assertEqual(self.db.query(ConversationMemorySummary).count(), 0)
        self.assertEqual(self.db.get(ChatSession, old.id).title, REDACTED_TITLE)
        self.assertEqual(old.messages[0].content, REDACTED_CONTENT)
        self.assertEqual(protected.messages[0].content, "原始敏感正文")
        reports = {item.risk_level: item for item in self.db.query(PsychologicalReport).all()}
        self.assertEqual(reports["LOW"].content, REDACTED_CONTENT)
        self.assertEqual(reports["HIGH"].content, "高风险正文")


if __name__ == "__main__":
    unittest.main()
