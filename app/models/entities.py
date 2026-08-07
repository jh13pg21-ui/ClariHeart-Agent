from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def now() -> datetime:
    return datetime.utcnow()


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(128))
    roles_csv: Mapped[str] = mapped_column(String(256), default="ROLE_USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    password_algorithm: Mapped[str] = mapped_column(String(32), default="legacy_sha256", server_default="legacy_sha256")
    must_reset_password: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    long_term_memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")

    @property
    def roles(self) -> list[str]:
        return [role for role in self.roles_csv.split(",") if role]

    @roles.setter
    def roles(self, value: list[str] | set[str]) -> None:
        self.roles_csv = ",".join(sorted(value))


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    long_term_memory_extracted_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    user: Mapped[UserAccount] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    conversation_summary: Mapped[Optional["ConversationMemorySummary"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def touch(self) -> None:
        self.updated_at = now()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class ConversationMemorySummary(Base):
    __tablename__ = "conversation_memory_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id"),
        unique=True,
        index=True,
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    through_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    source_message_count: Mapped[int] = mapped_column(Integer, default=0)
    model_provider: Mapped[str] = mapped_column(String(32), default="")
    model_name: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="structured-summary-v1")
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    scheduled_through_message_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    prompt_release: Mapped[str] = mapped_column(String(64), default="", server_default="")
    refresh_reason: Mapped[str] = mapped_column(String(64), default="", server_default="")

    session: Mapped[ChatSession] = relationship(back_populates="conversation_summary")


class LongTermMemory(Base):
    __tablename__ = "long_term_memories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "content_hash",
            name="uq_long_term_memories_user_content_hash",
        ),
        Index(
            "ix_long_term_memories_user_status_updated",
            "user_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id"),
        index=True,
    )
    source_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chat_sessions.id"),
        nullable=True,
        index=True,
    )
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="ACTIVE",
        server_default="ACTIVE",
        index=True,
    )
    evidence_message_ids_json: Mapped[str] = mapped_column(
        Text,
        default="[]",
        server_default="[]",
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5, server_default="0.5")
    extraction_method: Mapped[str] = mapped_column(
        String(32),
        default="legacy",
        server_default="legacy",
    )
    prompt_version: Mapped[str] = mapped_column(String(64), default="", server_default="")
    model_provider: Mapped[str] = mapped_column(String(32), default="", server_default="")
    model_name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    usage_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    supersedes_memory_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("long_term_memories.id"),
        nullable=True,
        index=True,
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(256), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    stable_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    document_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("knowledge_documents.id"), nullable=True, index=True
    )
    document_version_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("knowledge_document_versions.id"), nullable=True, index=True
    )
    parent_chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("knowledge_chunks.id"), nullable=True, index=True
    )
    chunk_kind: Mapped[str] = mapped_column(String(32), default="LEGACY_TEXT", server_default="LEGACY_TEXT", index=True)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    section_path_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    block_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    embedding_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    embedding_dimension: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", index=True)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(512), unique=True)
    display_name: Mapped[str] = mapped_column(String(256))
    mime_type: Mapped[str] = mapped_column(String(128))
    access_class: Mapped[str] = mapped_column(String(32), index=True)
    cloud_vision_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    active_version_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeDocumentVersion(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (UniqueConstraint("document_id", "sha256", name="uq_knowledge_doc_version_hash"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    pipeline_fingerprint: Mapped[str] = mapped_column(String(128))
    canonical_artifact_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    previous_version_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class KnowledgeIngestionJob(Base):
    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        Index("ix_knowledge_ingestion_jobs_version_status", "document_version_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_document_versions.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    progress_page: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_pages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_retryable: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    trigger_actor: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgePage(Base):
    __tablename__ = "knowledge_pages"
    __table_args__ = (
        UniqueConstraint("document_version_id", "page_number", name="uq_knowledge_page_version_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_document_versions.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    rotation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    text_strategy: Mapped[str] = mapped_column(String(32), index=True)
    structure_strategy: Mapped[str] = mapped_column(String(32), index=True)
    route_reasons_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quality_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    image_artifact_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    canonical_json: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PsychologicalReport(Base):
    __tablename__ = "psychological_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32))
    emotion: Mapped[str] = mapped_column(String(32))
    emotion_score: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RiskCase(Base):
    __tablename__ = "risk_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    risk_level: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    owner: Mapped[str] = mapped_column(String(128), default="unassigned")
    summary: Mapped[str] = mapped_column(Text)
    handoff_summary: Mapped[str] = mapped_column(Text, default="")
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CaseNote(Base):
    __tablename__ = "case_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AlertRecord(Base):
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ExcelRecord(Base):
    __tablename__ = "excel_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolJob(Base):
    __tablename__ = "tool_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    depends_on_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DeadLetterRecord(Base):
    __tablename__ = "dead_letter_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRunTrace(Base):
    __tablename__ = "agent_run_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(32), default="LOW", index=True)
    original_input: Mapped[str] = mapped_column(Text)
    sanitized_input: Mapped[str] = mapped_column(Text)
    memory_brief: Mapped[str] = mapped_column(Text, default="")
    agent_steps_json: Mapped[str] = mapped_column(Text, default="[]")
    retrieved_knowledge_json: Mapped[str] = mapped_column(Text, default="[]")
    response_messages_json: Mapped[str] = mapped_column(Text, default="[]")
    assessment_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ContextCompactionRecord(Base):
    __tablename__ = "context_compaction_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    task_name: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(256), default="")
    tokens_before: Mapped[int] = mapped_column(Integer)
    tokens_after: Mapped[int] = mapped_column(Integer)
    input_budget: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(128))
    layers_json: Mapped[str] = mapped_column(Text, default="[]")
    watermark: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), index=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ModelCallTrace(Base):
    __tablename__ = "model_call_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_accounts.id"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chat_sessions.id"),
        nullable=True,
        index=True,
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    task_name: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(256))
    risk_level: Mapped[str] = mapped_column(String(32), default="LOW", server_default="LOW")
    route: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(32), index=True)
    error_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    prompt_release: Mapped[str] = mapped_column(String(64), default="", server_default="")
    prompt_manifest_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    context_plan_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    context_section_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cloud_egress: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MemoryConsolidationRun(Base):
    __tablename__ = "memory_consolidation_runs"
    __table_args__ = (
        Index(
            "ix_memory_consolidation_runs_user_status_created",
            "user_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    trigger_reason: Mapped[str] = mapped_column(String(64), default="", server_default="")
    new_memory_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    modified_session_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_memory_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolAuditRecord(Base):
    __tablename__ = "tool_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    policy: Mapped[str] = mapped_column(String(128), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_token_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SecurityAuditRecord(Base):
    __tablename__ = "security_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user_accounts.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(128), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    __table_args__ = (UniqueConstraint("consumer", "message_id", name="uq_processed_messages_consumer_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumer: Mapped[str] = mapped_column(String(128))
    message_id: Mapped[str] = mapped_column(String(128))
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=now)

