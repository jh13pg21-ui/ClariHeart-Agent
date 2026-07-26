"""MindBridge 在引入 Alembic 前由 ORM ``create_all`` 创建的冻结结构。"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa


def _now() -> datetime:
    return datetime.utcnow()


LEGACY_METADATA = sa.MetaData()

user_accounts = sa.Table(
    "user_accounts",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("username", sa.String(64), nullable=False),
    sa.Column("display_name", sa.String(128), nullable=False),
    sa.Column("password_hash", sa.String(128), nullable=False),
    sa.Column("roles_csv", sa.String(256), nullable=False, default="ROLE_USER"),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

chat_sessions = sa.Table(
    "chat_sessions",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("public_id", sa.String(64), nullable=False),
    sa.Column("title", sa.String(160), nullable=False),
    sa.Column(
        "user_id",
        sa.Integer(),
        sa.ForeignKey("user_accounts.id"),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
    sa.Column("updated_at", sa.DateTime(), nullable=False, default=_now),
)

chat_messages = sa.Table(
    "chat_messages",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column(
        "user_id",
        sa.Integer(),
        sa.ForeignKey("user_accounts.id"),
        nullable=False,
    ),
    sa.Column(
        "session_id",
        sa.Integer(),
        sa.ForeignKey("chat_sessions.id"),
        nullable=False,
    ),
    sa.Column("role", sa.String(32), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

knowledge_chunks = sa.Table(
    "knowledge_chunks",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("source", sa.String(256), nullable=False),
    sa.Column("source_index", sa.Integer(), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("embedding_json", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

psychological_reports = sa.Table(
    "psychological_reports",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column(
        "user_id",
        sa.Integer(),
        sa.ForeignKey("user_accounts.id"),
        nullable=False,
    ),
    sa.Column(
        "session_id",
        sa.Integer(),
        sa.ForeignKey("chat_sessions.id"),
        nullable=False,
    ),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("intent", sa.String(32), nullable=False),
    sa.Column("emotion", sa.String(32), nullable=False),
    sa.Column("emotion_score", sa.Float(), nullable=False),
    sa.Column("risk_level", sa.String(32), nullable=False),
    sa.Column("confidence", sa.Float(), nullable=False),
    sa.Column("summary", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

risk_cases = sa.Table(
    "risk_cases",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("report_id", sa.Integer(), nullable=False),
    sa.Column("risk_level", sa.String(32), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("owner", sa.String(128), nullable=False, default="unassigned"),
    sa.Column("summary", sa.Text(), nullable=False),
    sa.Column("handoff_summary", sa.Text(), nullable=False, default=""),
    sa.Column("acknowledged_by", sa.String(128), nullable=True),
    sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
    sa.Column("updated_at", sa.DateTime(), nullable=False, default=_now),
)

case_notes = sa.Table(
    "case_notes",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("case_id", sa.Integer(), nullable=False),
    sa.Column("actor", sa.String(128), nullable=False),
    sa.Column("note", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

alert_records = sa.Table(
    "alert_records",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("report_id", sa.Integer(), nullable=False),
    sa.Column("channel", sa.String(64), nullable=False),
    sa.Column("recipient", sa.String(256), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

excel_records = sa.Table(
    "excel_records",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("report_id", sa.Integer(), nullable=False),
    sa.Column("file_path", sa.String(512), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

tool_jobs = sa.Table(
    "tool_jobs",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("report_id", sa.Integer(), nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False, default=0),
    sa.Column("max_attempts", sa.Integer(), nullable=False, default=3),
    sa.Column("depends_on_job_id", sa.Integer(), nullable=True),
    sa.Column("run_after", sa.DateTime(), nullable=False, default=_now),
    sa.Column("last_error", sa.Text(), nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
    sa.Column("updated_at", sa.DateTime(), nullable=False, default=_now),
)

dead_letter_records = sa.Table(
    "dead_letter_records",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("job_id", sa.Integer(), nullable=True),
    sa.Column("report_id", sa.Integer(), nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

agent_run_traces = sa.Table(
    "agent_run_traces",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column(
        "user_id",
        sa.Integer(),
        sa.ForeignKey("user_accounts.id"),
        nullable=False,
    ),
    sa.Column(
        "session_id",
        sa.Integer(),
        sa.ForeignKey("chat_sessions.id"),
        nullable=False,
    ),
    sa.Column("report_id", sa.Integer(), nullable=True),
    sa.Column("intent", sa.String(32), nullable=False),
    sa.Column("risk_level", sa.String(32), nullable=False, default="LOW"),
    sa.Column("original_input", sa.Text(), nullable=False),
    sa.Column("sanitized_input", sa.Text(), nullable=False),
    sa.Column("memory_brief", sa.Text(), nullable=False, default=""),
    sa.Column("agent_steps_json", sa.Text(), nullable=False, default="[]"),
    sa.Column(
        "retrieved_knowledge_json",
        sa.Text(),
        nullable=False,
        default="[]",
    ),
    sa.Column("response_messages_json", sa.Text(), nullable=False, default="[]"),
    sa.Column("assessment_json", sa.Text(), nullable=False, default="{}"),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
)

tool_audit_records = sa.Table(
    "tool_audit_records",
    LEGACY_METADATA,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("job_id", sa.Integer(), nullable=True),
    sa.Column("report_id", sa.Integer(), nullable=True),
    sa.Column("tool_name", sa.String(64), nullable=False),
    sa.Column("policy", sa.String(128), nullable=False, default=""),
    sa.Column("allowed", sa.Boolean(), nullable=False, default=True),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False, default=""),
    sa.Column("payload", sa.Text(), nullable=False, default="{}"),
    sa.Column("created_at", sa.DateTime(), nullable=False, default=_now),
    sa.Column("updated_at", sa.DateTime(), nullable=False, default=_now),
)

sa.Index("ix_user_accounts_username", user_accounts.c.username, unique=True)
sa.Index("ix_chat_sessions_public_id", chat_sessions.c.public_id, unique=True)
sa.Index("ix_knowledge_chunks_source", knowledge_chunks.c.source)
sa.Index("ix_risk_cases_report_id", risk_cases.c.report_id, unique=True)
sa.Index("ix_risk_cases_risk_level", risk_cases.c.risk_level)
sa.Index("ix_risk_cases_status", risk_cases.c.status)
sa.Index("ix_case_notes_case_id", case_notes.c.case_id)
sa.Index("ix_alert_records_report_id", alert_records.c.report_id)
sa.Index("ix_excel_records_report_id", excel_records.c.report_id)
sa.Index("ix_tool_jobs_report_id", tool_jobs.c.report_id)
sa.Index("ix_tool_jobs_kind", tool_jobs.c.kind)
sa.Index("ix_tool_jobs_status", tool_jobs.c.status)
sa.Index("ix_tool_jobs_depends_on_job_id", tool_jobs.c.depends_on_job_id)
sa.Index("ix_tool_jobs_run_after", tool_jobs.c.run_after)
sa.Index("ix_dead_letter_records_job_id", dead_letter_records.c.job_id)
sa.Index("ix_dead_letter_records_report_id", dead_letter_records.c.report_id)
sa.Index("ix_dead_letter_records_kind", dead_letter_records.c.kind)
sa.Index("ix_agent_run_traces_user_id", agent_run_traces.c.user_id)
sa.Index("ix_agent_run_traces_session_id", agent_run_traces.c.session_id)
sa.Index("ix_agent_run_traces_report_id", agent_run_traces.c.report_id)
sa.Index("ix_agent_run_traces_intent", agent_run_traces.c.intent)
sa.Index("ix_agent_run_traces_risk_level", agent_run_traces.c.risk_level)
sa.Index("ix_tool_audit_records_job_id", tool_audit_records.c.job_id)
sa.Index("ix_tool_audit_records_report_id", tool_audit_records.c.report_id)
sa.Index("ix_tool_audit_records_tool_name", tool_audit_records.c.tool_name)
sa.Index("ix_tool_audit_records_status", tool_audit_records.c.status)


def create_legacy_schema(bind) -> None:
    """在空数据库中创建冻结历史结构。"""
    LEGACY_METADATA.create_all(bind=bind, checkfirst=False)
