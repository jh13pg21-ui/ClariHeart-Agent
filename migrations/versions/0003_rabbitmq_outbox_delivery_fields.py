"""add RabbitMQ delivery fields to outbox events

Revision ID: 0003_rabbitmq_outbox
Revises: 0002_auth_audit_outbox_schema
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_rabbitmq_outbox"
down_revision = "0002_auth_audit_outbox_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch:
        batch.add_column(sa.Column("event_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(256), nullable=True))
        batch.add_column(sa.Column("published_at", sa.DateTime(), nullable=True))

    outbox = sa.table(
        "outbox_events",
        sa.column("id", sa.Integer()),
        sa.column("event_id", sa.String(64)),
        sa.column("idempotency_key", sa.String(256)),
        sa.column("status", sa.String(32)),
        sa.column("processed_at", sa.DateTime()),
        sa.column("published_at", sa.DateTime()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(outbox.c.id, outbox.c.status, outbox.c.processed_at)
    ).mappings()
    for row in rows:
        connection.execute(
            outbox.update()
            .where(outbox.c.id == row["id"])
            .values(
                event_id=f"legacy-{row['id']}",
                idempotency_key=f"legacy:{row['id']}",
                status=(row["status"] or "PENDING").upper(),
                published_at=row["processed_at"],
            )
        )

    with op.batch_alter_table("outbox_events") as batch:
        batch.alter_column("event_id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("idempotency_key", existing_type=sa.String(256), nullable=False)
        batch.create_index("ix_outbox_events_event_id", ["event_id"], unique=True)
        batch.create_index("ix_outbox_events_idempotency_key", ["idempotency_key"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch:
        batch.drop_index("ix_outbox_events_idempotency_key")
        batch.drop_index("ix_outbox_events_event_id")
        batch.drop_column("published_at")
        batch.drop_column("idempotency_key")
        batch.drop_column("event_id")
