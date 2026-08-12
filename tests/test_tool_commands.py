import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.core.enums import ToolJobKind
from app.models.entities import ChatSession, OutboxEvent, PsychologicalReport, ToolAuditRecord, UserAccount
from app.services.tool_commands import ToolCommandService


class ToolCommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.settings = Settings(app_environment="test", jwt_secret_key="test", database_url="sqlite:///:memory:")
        db = self.Session()
        user = UserAccount(username="student-command", display_name="Student", password_hash="hash")
        db.add(user)
        db.flush()
        session = ChatSession(public_id="command-session", title="command", user_id=user.id)
        db.add(session)
        db.flush()
        self.low = PsychologicalReport(
            user_id=user.id, session_id=session.id, content="low", intent="CONSULT",
            emotion="ANXIETY", emotion_score=1, risk_level="LOW", confidence=0.8, summary="low",
        )
        self.high = PsychologicalReport(
            user_id=user.id, session_id=session.id, content="high", intent="CONSULT",
            emotion="HIGH_RISK", emotion_score=4, risk_level="HIGH", confidence=0.9, summary="high",
        )
        db.add_all([self.low, self.high])
        db.commit()
        self.low_id = self.low.id
        self.high_id = self.high.id
        db.close()

    def tearDown(self):
        self.engine.dispose()

    def test_mcp_style_command_is_queued_idempotently(self):
        db = self.Session()
        service = ToolCommandService(db, self.settings)
        first = service.enqueue_report_command(self.high_id, ToolJobKind.ALERT_SEND.value)
        second = service.enqueue_report_command(self.high_id, ToolJobKind.ALERT_SEND.value)

        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(db.query(OutboxEvent).count(), 1)
        self.assertGreaterEqual(db.query(ToolAuditRecord).filter_by(status="QUEUED").count(), 1)
        db.close()

    def test_low_risk_alert_is_blocked_and_audited_at_command_boundary(self):
        db = self.Session()
        with self.assertRaises(PermissionError):
            ToolCommandService(db, self.settings).enqueue_report_command(self.low_id, ToolJobKind.ALERT_SEND.value)

        self.assertEqual(db.query(OutboxEvent).count(), 0)
        self.assertEqual(db.query(ToolAuditRecord).filter_by(status="BLOCKED").count(), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
