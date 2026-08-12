"""add long-term memory extraction watermark

Revision ID: 0005_memory_extraction_watermark
Revises: 0004_long_term_memory
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_memory_extraction_watermark"
down_revision = "0004_long_term_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("long_term_memory_extracted_message_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "long_term_memory_extracted_message_id")
