"""add langgraph trace identity

Revision ID: 0012_langgraph_trace_identity
Revises: 0011_memory_dream_v3
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_langgraph_trace_identity"
down_revision = "0011_memory_dream_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.add_column(sa.Column("turn_id", sa.String(length=64), server_default=sa.text("''"), nullable=False))
        batch.add_column(sa.Column("runtime_name", sa.String(length=32), server_default=sa.text("'langgraph'"), nullable=False))
        batch.create_index("ix_agent_run_traces_turn_id", ["turn_id"], unique=False)
        batch.create_index("ix_agent_run_traces_runtime_name", ["runtime_name"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.drop_index("ix_agent_run_traces_runtime_name")
        batch.drop_index("ix_agent_run_traces_turn_id")
        batch.drop_column("runtime_name")
        batch.drop_column("turn_id")
