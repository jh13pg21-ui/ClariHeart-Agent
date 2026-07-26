import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
LEGACY_TABLES = {
    "user_accounts", "chat_sessions", "chat_messages", "knowledge_chunks",
    "psychological_reports", "risk_cases", "case_notes", "alert_records",
    "excel_records", "tool_jobs", "dead_letter_records", "agent_run_traces",
    "tool_audit_records",
}
NEW_TABLES = {"auth_sessions", "security_audit_records", "outbox_events", "processed_messages"}


def create_legacy_schema(url: str) -> None:
    metadata = MetaData()
    Table("user_accounts", metadata, Column("id", Integer, primary_key=True), Column("username", String(64), unique=True), Column("display_name", String(128)), Column("password_hash", String(128)), Column("roles_csv", String(256)), Column("created_at", DateTime))
    Table("chat_sessions", metadata, Column("id", Integer, primary_key=True), Column("public_id", String(64), unique=True), Column("title", String(160)), Column("user_id", Integer), Column("created_at", DateTime), Column("updated_at", DateTime))
    Table("chat_messages", metadata, Column("id", Integer, primary_key=True), Column("user_id", Integer), Column("session_id", Integer), Column("role", String(32)), Column("content", Text), Column("created_at", DateTime))
    Table("knowledge_chunks", metadata, Column("id", Integer, primary_key=True), Column("source", String(256)), Column("source_index", Integer), Column("content", Text), Column("embedding_json", Text), Column("created_at", DateTime))
    Table("psychological_reports", metadata, Column("id", Integer, primary_key=True), Column("user_id", Integer), Column("session_id", Integer), Column("content", Text), Column("intent", String(32)), Column("emotion", String(32)), Column("emotion_score", Float), Column("risk_level", String(32)), Column("confidence", Float), Column("summary", Text), Column("created_at", DateTime))
    Table("risk_cases", metadata, Column("id", Integer, primary_key=True), Column("report_id", Integer), Column("risk_level", String(32)), Column("status", String(32)), Column("owner", String(128)), Column("summary", Text), Column("handoff_summary", Text), Column("acknowledged_by", String(128)), Column("acknowledged_at", DateTime), Column("created_at", DateTime), Column("updated_at", DateTime))
    Table("case_notes", metadata, Column("id", Integer, primary_key=True), Column("case_id", Integer), Column("actor", String(128)), Column("note", Text), Column("created_at", DateTime))
    Table("alert_records", metadata, Column("id", Integer, primary_key=True), Column("report_id", Integer), Column("channel", String(64)), Column("recipient", String(256)), Column("status", String(32)), Column("message", Text), Column("created_at", DateTime))
    Table("excel_records", metadata, Column("id", Integer, primary_key=True), Column("report_id", Integer), Column("file_path", String(512)), Column("status", String(32)), Column("message", Text), Column("created_at", DateTime))
    Table("tool_jobs", metadata, Column("id", Integer, primary_key=True), Column("report_id", Integer), Column("kind", String(64)), Column("status", String(32)), Column("attempts", Integer), Column("max_attempts", Integer), Column("depends_on_job_id", Integer), Column("run_after", DateTime), Column("last_error", Text), Column("created_at", DateTime), Column("updated_at", DateTime))
    Table("dead_letter_records", metadata, Column("id", Integer, primary_key=True), Column("job_id", Integer), Column("report_id", Integer), Column("kind", String(64)), Column("reason", Text), Column("payload", Text), Column("created_at", DateTime))
    Table("agent_run_traces", metadata, Column("id", Integer, primary_key=True), Column("user_id", Integer), Column("session_id", Integer), Column("report_id", Integer), Column("intent", String(32)), Column("risk_level", String(32)), Column("original_input", Text), Column("sanitized_input", Text), Column("memory_brief", Text), Column("agent_steps_json", Text), Column("retrieved_knowledge_json", Text), Column("response_messages_json", Text), Column("assessment_json", Text), Column("created_at", DateTime))
    Table("tool_audit_records", metadata, Column("id", Integer, primary_key=True), Column("job_id", Integer), Column("report_id", Integer), Column("tool_name", String(64)), Column("policy", String(128)), Column("allowed", Integer), Column("status", String(32)), Column("reason", Text), Column("payload", Text), Column("created_at", DateTime), Column("updated_at", DateTime))
    metadata.create_all(create_engine(url))


def run_upgrade(url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url
    return subprocess.run([sys.executable, "-m", "app.cli.migrate", "upgrade"], cwd=ROOT, env=environment, text=True, capture_output=True)


class MigrationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.empty_url = f"sqlite:///{Path(self.tempdir.name) / 'empty.db'}"
        self.legacy_url = f"sqlite:///{Path(self.tempdir.name) / 'legacy.db'}"
        self.unknown_url = f"sqlite:///{Path(self.tempdir.name) / 'unknown.db'}"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_upgrade_creates_schema_on_empty_database(self):
        result = run_upgrade(self.empty_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        inspector = inspect(create_engine(self.empty_url))
        self.assertTrue(LEGACY_TABLES | NEW_TABLES <= set(inspector.get_table_names()))
        columns = {column["name"] for column in inspector.get_columns("user_accounts")}
        self.assertTrue({"password_algorithm", "must_reset_password", "disabled"} <= columns)

    def test_upgrade_preserves_existing_rows(self):
        create_legacy_schema(self.legacy_url)
        engine = create_engine(self.legacy_url)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO user_accounts (id, username, display_name, password_hash, roles_csv) VALUES (1, 'student', 'Student', 'old-hash', 'ROLE_USER')"))
            connection.execute(text("INSERT INTO chat_messages (id, user_id, session_id, role, content) VALUES (1, 1, 1, 'user', 'keep this message')"))
        result = run_upgrade(self.legacy_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT content FROM chat_messages WHERE id = 1")).scalar_one(), "keep this message")
            self.assertEqual(connection.execute(text("SELECT password_hash FROM user_accounts WHERE id = 1")).scalar_one(), "old-hash")

    def test_matching_legacy_schema_is_stamped_then_upgraded_to_head(self):
        create_legacy_schema(self.legacy_url)
        result = run_upgrade(self.legacy_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        with create_engine(self.legacy_url).connect() as connection:
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "0002_auth_audit_outbox_schema")

    def test_unknown_or_incomplete_legacy_schema_is_rejected(self):
        engine = create_engine(self.unknown_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE user_accounts (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        result = run_upgrade(self.unknown_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("alembic_version", inspect(engine).get_table_names())

    def test_upgrade_is_idempotent(self):
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        with create_engine(self.empty_url).connect() as connection:
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one(), 1)

    def test_migration_head_matches_orm_new_tables_and_foreign_keys(self):
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        from app.core.database import Base
        import app.models.entities  # noqa: F401

        inspector = inspect(create_engine(self.empty_url))
        for name in NEW_TABLES:
            self.assertIn(name, Base.metadata.tables)
            self.assertIn(name, inspector.get_table_names())
            orm_foreign_keys = {foreign_key.target_fullname for foreign_key in Base.metadata.tables[name].foreign_keys}
            database_foreign_keys = {foreign_key["referred_table"] + "." + foreign_key["referred_columns"][0] for foreign_key in inspector.get_foreign_keys(name)}
            self.assertEqual(database_foreign_keys, orm_foreign_keys)
