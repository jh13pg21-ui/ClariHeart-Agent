import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import (
    AlertRecord,
    ChatSession,
    ExcelRecord,
    ProcessedMessage,
    PsychologicalReport,
    RiskCase,
    ToolAuditRecord,
    UserAccount,
    DeadLetterRecord,
    ToolJob,
)
from app.workers import tasks


class WorkerTaskTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.ledger = Path(".tmp-worker-tests.xlsx").resolve()
        if self.ledger.exists():
            self.ledger.unlink()
        self.settings = Settings(
            app_environment="test",
            jwt_secret_key="test",
            database_url="sqlite:///:memory:",
            excel_path=str(self.ledger),
            alert_email_delivery_mode="log",
        )
        db = self.Session()
        user = UserAccount(
            username="worker-user",
            display_name="Worker User",
            password_hash="hash",
        )
        db.add(user)
        db.flush()
        session = ChatSession(public_id="worker-session", title="test", user_id=user.id)
        db.add(session)
        db.flush()
        self.low_report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content="low",
            intent="CONSULT",
            emotion="ANXIETY",
            emotion_score=0.2,
            risk_level="LOW",
            confidence=0.8,
            summary="low risk",
        )
        self.high_report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content="high",
            intent="RISK",
            emotion="HIGH_RISK",
            emotion_score=0.9,
            risk_level="HIGH",
            confidence=0.9,
            summary="high risk",
        )
        db.add_all([self.low_report, self.high_report])
        db.commit()
        self.low_report_id = self.low_report.id
        self.high_report_id = self.high_report.id
        db.close()

    def tearDown(self):
        self.engine.dispose()
        if self.ledger.exists():
            self.ledger.unlink()

    def test_duplicate_excel_event_has_one_side_effect(self):
        with patch.object(tasks, "SessionLocal", self.Session), patch.object(tasks, "worker_settings", self.settings):
            first = tasks.process_excel.run("event-excel", self.low_report_id)
            second = tasks.process_excel.run("event-excel", self.low_report_id)

        db = self.Session()
        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(second["status"], "ALREADY_PROCESSED")
        self.assertEqual(db.query(ExcelRecord).count(), 1)
        self.assertEqual(db.query(ProcessedMessage).count(), 1)
        db.close()

    def test_low_risk_alert_is_blocked_and_audited(self):
        db = self.Session()
        case = RiskCase(
            report_id=self.low_report_id,
            risk_level="LOW",
            status="OPEN",
            owner="owner",
            summary="low",
            handoff_summary="handoff",
        )
        db.add(case)
        db.commit()
        case_id = case.id
        db.close()

        with patch.object(tasks, "SessionLocal", self.Session), patch.object(tasks, "worker_settings", self.settings):
            result = tasks.send_high_risk_alert.run("event-alert", case_id)

        db = self.Session()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(db.query(AlertRecord).count(), 0)
        self.assertEqual(
            db.query(ToolAuditRecord).filter_by(status="BLOCKED").count(),
            1,
        )
        db.close()

    def test_case_creation_emits_high_risk_case_created_event_once(self):
        with patch.object(tasks, "SessionLocal", self.Session), patch.object(tasks, "worker_settings", self.settings):
            first = tasks.create_case.run("event-case", self.high_report_id)
            second = tasks.create_case.run("event-case", self.high_report_id)

        db = self.Session()
        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(second["status"], "ALREADY_PROCESSED")
        self.assertEqual(db.query(RiskCase).filter_by(report_id=self.high_report_id).count(), 1)
        from app.models.entities import OutboxEvent

        self.assertEqual(db.query(OutboxEvent).filter_by(event_type="case.created").count(), 1)
        db.close()

    def test_retry_exhaustion_creates_dead_letter_and_terminal_job(self):
        self.settings.tool_task_max_attempts = 1
        failed_record = type("Record", (), {"status": "FAILED", "message": "磁盘暂时不可写"})()
        with (
            patch.object(tasks, "SessionLocal", self.Session),
            patch.object(tasks, "worker_settings", self.settings),
            patch.object(tasks.ToolOrchestrationService, "write_excel", return_value=failed_record),
        ):
            result = tasks.process_excel.run("event-dead", self.low_report_id)

        db = self.Session()
        self.assertEqual(result["status"], "DEAD")
        self.assertEqual(db.query(DeadLetterRecord).count(), 1)
        self.assertEqual(db.query(ToolJob).filter_by(status="DEAD").count(), 1)
        db.close()

    def test_alert_rate_limit_is_enforced_before_delivery(self):
        self.settings.tool_task_max_attempts = 1
        self.settings.alert_email_rate_limit_per_minute = 1
        db = self.Session()
        db.add(AlertRecord(report_id=self.low_report_id, channel="email", recipient="x", status="SUCCESS", message="sent"))
        case = RiskCase(
            report_id=self.high_report_id,
            risk_level="HIGH",
            status="OPEN",
            owner="owner",
            summary="high",
            handoff_summary="handoff",
        )
        db.add(case)
        db.commit()
        case_id = case.id
        db.close()

        with patch.object(tasks, "SessionLocal", self.Session), patch.object(tasks, "worker_settings", self.settings):
            result = tasks.send_high_risk_alert.run("event-limited", case_id)

        db = self.Session()
        self.assertEqual(result["status"], "DEAD")
        self.assertIn("限流", db.query(DeadLetterRecord).one().reason)
        db.close()


if __name__ == "__main__":
    unittest.main()
