"""add memory dream v3 conflict and lease metadata

Revision ID: 0011_memory_dream_v3
Revises: 0010_memory_v2_traces
"""

from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa


revision = "0011_memory_dream_v3"
down_revision = "0010_memory_v2_traces"
branch_labels = None
depends_on = None


def _memory_key(memory_type: str, name: str) -> str:
    normalized_type = re.sub(r"[^a-z0-9]+", "-", str(memory_type or "context").lower()).strip("-")
    normalized_name = re.sub(r"[^\w]+", "-", str(name or "memory").lower(), flags=re.UNICODE).strip("-_")
    return f"{normalized_type or 'context'}.{normalized_name or 'memory'}"[:191]


def upgrade() -> None:
    with op.batch_alter_table("long_term_memories") as batch:
        batch.add_column(sa.Column("memory_key", sa.String(length=191), nullable=True))
        batch.add_column(sa.Column("conflict_group_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("consolidated_from_ids_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("resolution_reason", sa.Text(), nullable=True))

    connection = op.get_bind()
    memories = sa.table(
        "long_term_memories",
        sa.column("id", sa.Integer()),
        sa.column("memory_type", sa.String()),
        sa.column("name", sa.String()),
        sa.column("memory_key", sa.String()),
        sa.column("consolidated_from_ids_json", sa.Text()),
        sa.column("resolution_reason", sa.Text()),
    )
    rows = connection.execute(
        sa.select(memories.c.id, memories.c.memory_type, memories.c.name)
    ).mappings()
    for row in rows:
        connection.execute(
            memories.update()
            .where(memories.c.id == row["id"])
            .values(
                memory_key=_memory_key(row["memory_type"], row["name"]),
                consolidated_from_ids_json="[]",
                resolution_reason="",
            )
        )

    with op.batch_alter_table("long_term_memories") as batch:
        batch.alter_column("memory_key", existing_type=sa.String(length=191), nullable=False)
        batch.alter_column("consolidated_from_ids_json", existing_type=sa.Text(), nullable=False)
        batch.alter_column("resolution_reason", existing_type=sa.Text(), nullable=False)
        batch.create_index("ix_long_term_memories_memory_key", ["memory_key"], unique=False)
        batch.create_index("ix_long_term_memories_conflict_group_id", ["conflict_group_id"], unique=False)
        batch.create_index(
            "ix_long_term_memories_user_key_status",
            ["user_id", "memory_key", "status"],
            unique=False,
        )

    op.create_table(
        "memory_dream_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(), nullable=True),
        sa.Column("last_consolidated_at", sa.DateTime(), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), server_default="", nullable=False),
        sa.Column("lease_acquired_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_memory_dream_states_user_id"),
    )
    op.create_index("ix_memory_dream_states_user_id", "memory_dream_states", ["user_id"], unique=False)
    op.create_index(
        "ix_memory_dream_states_lease_expires_at",
        "memory_dream_states",
        ["lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_dream_states_lease_expires_at", table_name="memory_dream_states")
    op.drop_index("ix_memory_dream_states_user_id", table_name="memory_dream_states")
    op.drop_table("memory_dream_states")

    with op.batch_alter_table("long_term_memories") as batch:
        batch.drop_index("ix_long_term_memories_user_key_status")
        batch.drop_index("ix_long_term_memories_conflict_group_id")
        batch.drop_index("ix_long_term_memories_memory_key")
        batch.drop_column("resolution_reason")
        batch.drop_column("consolidated_from_ids_json")
        batch.drop_column("conflict_group_id")
        batch.drop_column("memory_key")
