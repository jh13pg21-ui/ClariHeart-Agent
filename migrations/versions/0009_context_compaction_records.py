"""add context compaction audit records

Revision ID: 0009_context_compaction
Revises: 0008_rag_ingestion
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_context_compaction"
down_revision = "0008_rag_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_compaction_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after", sa.Integer(), nullable=False),
        sa.Column("input_budget", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("layers_json", sa.Text(), nullable=False),
        sa.Column("watermark", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_compaction_records_request_id", "context_compaction_records", ["request_id"])
    op.create_index("ix_context_compaction_records_session_id", "context_compaction_records", ["session_id"])
    op.create_index("ix_context_compaction_records_agent_name", "context_compaction_records", ["agent_name"])
    op.create_index("ix_context_compaction_records_task_name", "context_compaction_records", ["task_name"])
    op.create_index("ix_context_compaction_records_status", "context_compaction_records", ["status"])
    op.create_index("ix_context_compaction_records_manifest_hash", "context_compaction_records", ["manifest_hash"])


def downgrade() -> None:
    op.drop_index("ix_context_compaction_records_manifest_hash", table_name="context_compaction_records")
    op.drop_index("ix_context_compaction_records_status", table_name="context_compaction_records")
    op.drop_index("ix_context_compaction_records_task_name", table_name="context_compaction_records")
    op.drop_index("ix_context_compaction_records_agent_name", table_name="context_compaction_records")
    op.drop_index("ix_context_compaction_records_session_id", table_name="context_compaction_records")
    op.drop_index("ix_context_compaction_records_request_id", table_name="context_compaction_records")
    op.drop_table("context_compaction_records")
