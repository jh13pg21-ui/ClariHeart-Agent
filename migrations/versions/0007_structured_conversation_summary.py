"""add structured conversation summary checkpoints

Revision ID: 0007_structured_summary
Revises: 0006_privacy_controls
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_structured_summary"
down_revision = "0006_privacy_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_memory_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("through_message_id", sa.Integer(), nullable=True),
        sa.Column("source_message_count", sa.Integer(), nullable=False),
        sa.Column("model_provider", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_memory_summaries_session_id",
        "conversation_memory_summaries",
        ["session_id"],
        unique=True,
    )
    op.create_index(
        "ix_conversation_memory_summaries_through_message_id",
        "conversation_memory_summaries",
        ["through_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_memory_summaries_status",
        "conversation_memory_summaries",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_memory_summaries_status",
        table_name="conversation_memory_summaries",
    )
    op.drop_index(
        "ix_conversation_memory_summaries_through_message_id",
        table_name="conversation_memory_summaries",
    )
    op.drop_index(
        "ix_conversation_memory_summaries_session_id",
        table_name="conversation_memory_summaries",
    )
    op.drop_table("conversation_memory_summaries")
