"""auth audit and outbox schema

Revision ID: 0002_auth_audit_outbox_schema
Revises: 0001_existing_schema_baseline
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_auth_audit_outbox_schema"
down_revision = "0001_existing_schema_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_accounts") as batch:
        batch.add_column(sa.Column("password_algorithm", sa.String(32), nullable=False, server_default="legacy_sha256"))
        batch.add_column(sa.Column("must_reset_password", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("auth_sessions", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False), sa.Column("session_token_id", sa.String(128), nullable=False), sa.Column("issued_at", sa.DateTime(), nullable=False), sa.Column("expires_at", sa.DateTime(), nullable=False), sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("revoked_at", sa.DateTime(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_session_token_id", "auth_sessions", ["session_token_id"], unique=True)
    op.create_table("security_audit_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=True), sa.Column("actor", sa.String(128), nullable=False), sa.Column("action", sa.String(64), nullable=False), sa.Column("resource_type", sa.String(64), nullable=False), sa.Column("resource_id", sa.String(128), nullable=False), sa.Column("ip_address", sa.String(64), nullable=False), sa.Column("details_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_security_audit_records_user_id", "security_audit_records", ["user_id"])
    op.create_index("ix_security_audit_records_action", "security_audit_records", ["action"])
    op.create_table("outbox_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("event_type", sa.String(128), nullable=False), sa.Column("aggregate_type", sa.String(64), nullable=False), sa.Column("aggregate_id", sa.String(128), nullable=False), sa.Column("payload_json", sa.Text(), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("available_at", sa.DateTime(), nullable=False), sa.Column("processed_at", sa.DateTime(), nullable=True), sa.Column("last_error", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_outbox_events_event_type", "outbox_events", ["event_type"])
    op.create_index("ix_outbox_events_aggregate_id", "outbox_events", ["aggregate_id"])
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    op.create_index("ix_outbox_events_available_at", "outbox_events", ["available_at"])
    op.create_table("processed_messages", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("consumer", sa.String(128), nullable=False), sa.Column("message_id", sa.String(128), nullable=False), sa.Column("processed_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("consumer", "message_id", name="uq_processed_messages_consumer_message"))


def downgrade() -> None:
    op.drop_table("processed_messages")
    op.drop_table("outbox_events")
    op.drop_table("security_audit_records")
    op.drop_table("auth_sessions")
    with op.batch_alter_table("user_accounts") as batch:
        batch.drop_column("disabled")
        batch.drop_column("must_reset_password")
        batch.drop_column("password_algorithm")
