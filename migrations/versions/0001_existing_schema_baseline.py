"""existing schema baseline

Revision ID: 0001_existing_schema_baseline
Revises:
"""
from alembic import op

from migrations.legacy_schema import create_legacy_schema


revision = "0001_existing_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_legacy_schema(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("baseline migration is intentionally irreversible")
