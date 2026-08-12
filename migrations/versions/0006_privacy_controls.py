"""add user privacy controls

Revision ID: 0006_privacy_controls
Revises: 0005_memory_extraction_watermark
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_privacy_controls"
down_revision = "0005_memory_extraction_watermark"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_accounts",
        sa.Column("long_term_memory_enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("user_accounts", "long_term_memory_enabled")
