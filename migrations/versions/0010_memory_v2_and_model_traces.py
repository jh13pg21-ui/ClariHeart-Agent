"""add memory v2 metadata and privacy-safe model traces

Revision ID: 0010_memory_v2_traces
Revises: 0009_context_compaction
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_memory_v2_traces"
down_revision = "0009_context_compaction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_memory_summaries") as batch:
        batch.add_column(sa.Column("scheduled_through_message_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("scheduled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("prompt_release", sa.String(length=64), server_default="", nullable=False))
        batch.add_column(sa.Column("refresh_reason", sa.String(length=64), server_default="", nullable=False))
        batch.create_index(
            "ix_conversation_memory_summaries_scheduled_through_message_id",
            ["scheduled_through_message_id"],
            unique=False,
        )

    with op.batch_alter_table("long_term_memories") as batch:
        batch.add_column(sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False))
        batch.add_column(sa.Column("evidence_message_ids_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("confidence", sa.Float(), server_default="0.5", nullable=False))
        batch.add_column(sa.Column("extraction_method", sa.String(length=32), server_default="legacy", nullable=False))
        batch.add_column(sa.Column("prompt_version", sa.String(length=64), server_default="", nullable=False))
        batch.add_column(sa.Column("model_provider", sa.String(length=32), server_default="", nullable=False))
        batch.add_column(sa.Column("model_name", sa.String(length=128), server_default="", nullable=False))
        batch.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))
        batch.add_column(sa.Column("usage_count", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("confirmation_count", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("last_confirmed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("supersedes_memory_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_long_term_memories_supersedes_memory_id",
            "long_term_memories",
            ["supersedes_memory_id"],
            ["id"],
        )
        batch.create_index("ix_long_term_memories_status", ["status"], unique=False)
        batch.create_index("ix_long_term_memories_expires_at", ["expires_at"], unique=False)
        batch.create_index(
            "ix_long_term_memories_supersedes_memory_id",
            ["supersedes_memory_id"],
            unique=False,
        )
        batch.create_index(
            "ix_long_term_memories_user_status_updated",
            ["user_id", "status", "updated_at"],
            unique=False,
        )

    op.execute(
        sa.text(
            "UPDATE long_term_memories "
            "SET evidence_message_ids_json = '[]' "
            "WHERE evidence_message_ids_json IS NULL"
        )
    )
    with op.batch_alter_table("long_term_memories") as batch:
        batch.alter_column(
            "evidence_message_ids_json",
            existing_type=sa.Text(),
            nullable=False,
        )

    op.create_table(
        "model_call_traces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("risk_level", sa.String(length=32), server_default="LOW", nullable=False),
        sa.Column("route", sa.String(length=64), server_default="", nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), server_default="", nullable=False),
        sa.Column("prompt_release", sa.String(length=64), server_default="", nullable=False),
        sa.Column("prompt_manifest_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("context_plan_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("context_section_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Float(), server_default="0", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cloud_egress", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("request_id", "user_id", "session_id", "agent_name", "task_name", "provider", "status"):
        op.create_index(f"ix_model_call_traces_{column}", "model_call_traces", [column], unique=False)

    op.create_table(
        "memory_consolidation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trigger_reason", sa.String(length=64), server_default="", nullable=False),
        sa.Column("new_memory_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("modified_session_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_memory_ids_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_consolidation_runs_public_id", "memory_consolidation_runs", ["public_id"], unique=True)
    op.create_index("ix_memory_consolidation_runs_user_id", "memory_consolidation_runs", ["user_id"], unique=False)
    op.create_index("ix_memory_consolidation_runs_status", "memory_consolidation_runs", ["status"], unique=False)
    op.create_index(
        "ix_memory_consolidation_runs_user_status_created",
        "memory_consolidation_runs",
        ["user_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_consolidation_runs_user_status_created", table_name="memory_consolidation_runs")
    op.drop_index("ix_memory_consolidation_runs_status", table_name="memory_consolidation_runs")
    op.drop_index("ix_memory_consolidation_runs_user_id", table_name="memory_consolidation_runs")
    op.drop_index("ix_memory_consolidation_runs_public_id", table_name="memory_consolidation_runs")
    op.drop_table("memory_consolidation_runs")

    for column in reversed(("request_id", "user_id", "session_id", "agent_name", "task_name", "provider", "status")):
        op.drop_index(f"ix_model_call_traces_{column}", table_name="model_call_traces")
    op.drop_table("model_call_traces")

    with op.batch_alter_table("long_term_memories") as batch:
        batch.drop_index("ix_long_term_memories_user_status_updated")
        batch.drop_index("ix_long_term_memories_supersedes_memory_id")
        batch.drop_index("ix_long_term_memories_expires_at")
        batch.drop_index("ix_long_term_memories_status")
        batch.drop_constraint("fk_long_term_memories_supersedes_memory_id", type_="foreignkey")
        for column in (
            "supersedes_memory_id",
            "expires_at",
            "last_confirmed_at",
            "confirmation_count",
            "usage_count",
            "version",
            "model_name",
            "model_provider",
            "prompt_version",
            "extraction_method",
            "confidence",
            "evidence_message_ids_json",
            "status",
        ):
            batch.drop_column(column)

    with op.batch_alter_table("conversation_memory_summaries") as batch:
        batch.drop_index("ix_conversation_memory_summaries_scheduled_through_message_id")
        for column in (
            "refresh_reason",
            "prompt_release",
            "output_tokens",
            "input_tokens",
            "scheduled_at",
            "scheduled_through_message_id",
        ):
            batch.drop_column(column)
