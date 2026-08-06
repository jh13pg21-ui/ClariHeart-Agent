from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.entities import AgentRunTrace, ChatMessage, ChatSession, ConversationMemorySummary, PsychologicalReport


REDACTED_CONTENT = "[内容已按数据保留策略清除]"
REDACTED_TITLE = "已过保留期的会话"


@dataclass(frozen=True)
class RetentionResult:
    messages: int = 0
    sessions: int = 0
    reports: int = 0
    traces: int = 0
    summaries: int = 0


class PrivacyRetentionService:
    """按风险等级执行内容最小化，保留审计元数据而清除过期正文。"""

    def __init__(self, db: Session, settings):
        self.db = db
        self.settings = settings

    def purge_expired(self, now: datetime | None = None) -> RetentionResult:
        if not getattr(self.settings, "privacy_retention_enabled", True):
            return RetentionResult()
        current = now or datetime.utcnow()
        chat_cutoff = current - timedelta(
            days=max(1, int(getattr(self.settings, "chat_data_retention_days", 365)))
        )
        risk_cutoff = current - timedelta(
            days=max(1, int(getattr(self.settings, "risk_data_retention_days", 1095)))
        )
        reports = traces = messages = sessions = summaries = 0

        for report in self.db.query(PsychologicalReport).all():
            cutoff = risk_cutoff if report.risk_level in {"MEDIUM", "HIGH"} else chat_cutoff
            if report.created_at < cutoff and report.content != REDACTED_CONTENT:
                report.content = REDACTED_CONTENT
                report.summary = REDACTED_CONTENT
                self.db.add(report)
                reports += 1

        for trace in self.db.query(AgentRunTrace).all():
            cutoff = risk_cutoff if trace.risk_level in {"MEDIUM", "HIGH"} else chat_cutoff
            if trace.created_at < cutoff and trace.original_input != REDACTED_CONTENT:
                trace.original_input = REDACTED_CONTENT
                trace.sanitized_input = REDACTED_CONTENT
                trace.memory_brief = ""
                trace.retrieved_knowledge_json = "[]"
                trace.response_messages_json = "[]"
                trace.assessment_json = "{}"
                self.db.add(trace)
                traces += 1

        protected_sessions = {
            report.session_id
            for report in self.db.query(PsychologicalReport).filter(
                PsychologicalReport.risk_level.in_(("MEDIUM", "HIGH")),
                PsychologicalReport.created_at >= risk_cutoff,
            )
        }
        for message in self.db.query(ChatMessage).filter(ChatMessage.created_at < chat_cutoff).all():
            if message.session_id not in protected_sessions and message.content != REDACTED_CONTENT:
                message.content = REDACTED_CONTENT
                self.db.add(message)
                messages += 1

        for session in self.db.query(ChatSession).filter(ChatSession.updated_at < chat_cutoff).all():
            if session.id not in protected_sessions and session.title != REDACTED_TITLE:
                summaries += (
                    self.db.query(ConversationMemorySummary)
                    .filter(ConversationMemorySummary.session_id == session.id)
                    .delete(synchronize_session=False)
                )
                session.title = REDACTED_TITLE
                self.db.add(session)
                sessions += 1

        self.db.commit()
        return RetentionResult(
            messages=messages,
            sessions=sessions,
            reports=reports,
            traces=traces,
            summaries=summaries,
        )
