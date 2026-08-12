"""add user-scoped long term memories

Revision ID: 0004_long_term_memory
Revises: 0003_rabbitmq_outbox
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_long_term_memory"
down_revision = "0003_rabbitmq_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "long_term_memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_session_id", sa.Integer(), nullable=True),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["source_session_id"], ["chat_sessions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "content_hash",
            name="uq_long_term_memories_user_content_hash",
        ),
    )
    op.create_index(
        "ix_long_term_memories_public_id",
        "long_term_memories",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_long_term_memories_user_id",
        "long_term_memories",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_long_term_memories_source_session_id",
        "long_term_memories",
        ["source_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_long_term_memories_memory_type",
        "long_term_memories",
        ["memory_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_long_term_memories_memory_type",
        table_name="long_term_memories",
    )
    op.drop_index(
        "ix_long_term_memories_source_session_id",
        table_name="long_term_memories",
    )
    op.drop_index(
        "ix_long_term_memories_user_id",
        table_name="long_term_memories",
    )
    op.drop_index(
        "ix_long_term_memories_public_id",
        table_name="long_term_memories",
    )
    op.drop_table("long_term_memories")
