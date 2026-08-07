import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import Column, DateTime, DefaultClause, Float, Integer, MetaData, String, Table, Text, create_engine, inspect, text

from app.cli.migrate import BASELINE_REVISION, alembic_config
from migrations.legacy_schema import LEGACY_METADATA


ROOT = Path(__file__).resolve().parents[1]
LEGACY_TABLES = {
    "user_accounts", "chat_sessions", "chat_messages", "knowledge_chunks",
    "psychological_reports", "risk_cases", "case_notes", "alert_records",
    "excel_records", "tool_jobs", "dead_letter_records", "agent_run_traces",
    "tool_audit_records",
}
NEW_TABLES = {
    "auth_sessions",
    "security_audit_records",
    "outbox_events",
    "processed_messages",
    "long_term_memories",
    "conversation_memory_summaries",
    "knowledge_documents",
    "knowledge_document_versions",
    "knowledge_ingestion_jobs",
    "knowledge_pages",
    "context_compaction_records",
}


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
    engine = create_engine(url)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()


def create_exact_legacy_schema(url: str) -> None:
    engine = create_engine(url)
    try:
        LEGACY_METADATA.create_all(engine)
    finally:
        engine.dispose()


def create_legacy_schema_with_server_default_drift(url: str) -> None:
    metadata = MetaData()
    for table in LEGACY_METADATA.sorted_tables:
        table.to_metadata(metadata)
    metadata.tables["user_accounts"].c.roles_csv.server_default = DefaultClause("ROLE_USER")
    engine = create_engine(url)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()


def create_legacy_schema_with_foreign_key_option_drift(url: str) -> None:
    metadata = MetaData()
    for table in LEGACY_METADATA.sorted_tables:
        table.to_metadata(metadata)
    foreign_key = next(
        iter(metadata.tables["chat_sessions"].c.user_id.foreign_keys)
    )
    foreign_key.constraint.ondelete = "CASCADE"
    engine = create_engine(url)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()


def schema_signature(url: str) -> dict:
    def normalized_default(value):
        if value is None:
            return None
        normalized = str(value).strip()
        while (
            len(normalized) >= 2
            and normalized[0] == "("
            and normalized[-1] == ")"
        ):
            normalized = normalized[1:-1].strip()
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in "'\""
        ):
            normalized = normalized[1:-1]
        return normalized

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        signature = {}
        for table in sorted(
            table
            for table in inspector.get_table_names()
            if table != "alembic_version"
        ):
            primary_keys = set(
                inspector.get_pk_constraint(table).get("constrained_columns") or ()
            )
            signature[table] = {
                "columns": tuple(
                    (
                        column["name"],
                        str(column["type"]),
                        column["nullable"],
                        normalized_default(column["default"]),
                        column["name"] in primary_keys,
                    )
                    for column in inspector.get_columns(table)
                ),
                "foreign_keys": tuple(
                    sorted(
                        (
                            tuple(foreign_key["constrained_columns"]),
                            foreign_key["referred_table"],
                            tuple(foreign_key["referred_columns"]),
                        )
                        for foreign_key in inspector.get_foreign_keys(table)
                    )
                ),
                "unique": tuple(
                    sorted(
                        (
                            item["name"],
                            tuple(item["column_names"]),
                        )
                        for item in inspector.get_unique_constraints(table)
                    )
                ),
                "indexes": tuple(
                    sorted(
                        (
                            item["name"],
                            tuple(item["column_names"]),
                            bool(item["unique"]),
                        )
                        for item in inspector.get_indexes(table)
                    )
                ),
            }
        return signature
    finally:
        engine.dispose()


def run_upgrade(url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url
    return subprocess.run([sys.executable, "-m", "app.cli.migrate", "upgrade"], cwd=ROOT, env=environment, text=True, capture_output=True)


def run_check(url: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url
    return subprocess.run([sys.executable, "-m", "app.cli.migrate", "check"], cwd=ROOT, env=environment, text=True, capture_output=True)


def table_names(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        return inspect(engine).get_table_names()
    finally:
        engine.dispose()


def scalar_sql(url: str, statement: str):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(text(statement)).scalar_one()
    finally:
        engine.dispose()


class MigrationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.empty_url = f"sqlite:///{Path(self.tempdir.name) / 'empty.db'}"
        self.legacy_url = f"sqlite:///{Path(self.tempdir.name) / 'legacy.db'}"
        self.unknown_url = f"sqlite:///{Path(self.tempdir.name) / 'unknown.db'}"
        self.reference_url = f"sqlite:///{Path(self.tempdir.name) / 'reference.db'}"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_upgrade_creates_schema_on_empty_database(self):
        result = run_upgrade(self.empty_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        engine = create_engine(self.empty_url)
        try:
            inspector = inspect(engine)
            self.assertTrue(LEGACY_TABLES | NEW_TABLES <= set(inspector.get_table_names()))
            columns = {column["name"] for column in inspector.get_columns("user_accounts")}
            self.assertTrue({"password_algorithm", "must_reset_password", "disabled"} <= columns)
        finally:
            engine.dispose()

    def test_upgrade_preserves_existing_rows(self):
        create_exact_legacy_schema(self.legacy_url)
        engine = create_engine(self.legacy_url)
        try:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO user_accounts (id, username, display_name, password_hash, roles_csv, created_at) VALUES (1, 'student', 'Student', 'old-hash', 'ROLE_USER', CURRENT_TIMESTAMP)"))
                connection.execute(text("INSERT INTO chat_sessions (id, public_id, title, user_id, created_at, updated_at) VALUES (1, 'legacy-session', 'Legacy', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
                connection.execute(text("INSERT INTO chat_messages (id, user_id, session_id, role, content, created_at) VALUES (1, 1, 1, 'user', 'keep this message', CURRENT_TIMESTAMP)"))
            result = run_upgrade(self.legacy_url)
            self.assertEqual(result.returncode, 0, result.stderr)
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT content FROM chat_messages WHERE id = 1")).scalar_one(), "keep this message")
                self.assertEqual(connection.execute(text("SELECT password_hash FROM user_accounts WHERE id = 1")).scalar_one(), "old-hash")
        finally:
            engine.dispose()

    def test_matching_legacy_schema_is_stamped_then_upgraded_to_head(self):
        create_exact_legacy_schema(self.legacy_url)
        result = run_upgrade(self.legacy_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            scalar_sql(self.legacy_url, "SELECT version_num FROM alembic_version"),
            "0009_context_compaction",
        )

    def test_unknown_or_incomplete_legacy_schema_is_rejected(self):
        engine = create_engine(self.unknown_url)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE user_accounts (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        result = run_upgrade(self.unknown_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("alembic_version", inspect(engine).get_table_names())
        engine.dispose()

    def test_same_names_and_columns_with_legacy_constraint_drift_is_rejected(self):
        create_legacy_schema(self.legacy_url)
        result = run_upgrade(self.legacy_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("alembic_version", table_names(self.legacy_url))

    def test_legacy_index_drift_is_rejected_without_version_table(self):
        create_exact_legacy_schema(self.legacy_url)
        engine = create_engine(self.legacy_url)
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_tool_jobs_status"))
            connection.execute(
                text(
                    "CREATE INDEX ix_wrong_tool_jobs_status "
                    "ON tool_jobs (status)"
                )
            )
        result = run_upgrade(self.legacy_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("alembic_version", inspect(engine).get_table_names())
        engine.dispose()

    def test_legacy_server_default_drift_is_rejected_without_version_table(self):
        create_legacy_schema_with_server_default_drift(self.legacy_url)
        result = run_upgrade(self.legacy_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(
            "alembic_version",
            table_names(self.legacy_url),
        )

    def test_legacy_foreign_key_option_drift_is_rejected_without_version_table(self):
        create_legacy_schema_with_foreign_key_option_drift(self.legacy_url)
        result = run_upgrade(self.legacy_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(
            "alembic_version",
            table_names(self.legacy_url),
        )

    def test_upgrade_is_idempotent(self):
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        self.assertEqual(
            scalar_sql(self.empty_url, "SELECT COUNT(*) FROM alembic_version"),
            1,
        )

    def test_baseline_revision_matches_frozen_historical_schema(self):
        command.upgrade(alembic_config(self.empty_url), BASELINE_REVISION)
        create_exact_legacy_schema(self.reference_url)
        self.assertEqual(
            schema_signature(self.empty_url),
            schema_signature(self.reference_url),
        )

    def test_migration_head_matches_current_orm_schema(self):
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        from app.core.database import Base
        import app.models.entities  # noqa: F401

        reference_engine = create_engine(self.reference_url)
        try:
            Base.metadata.create_all(reference_engine)
        finally:
            reference_engine.dispose()
        self.assertEqual(
            schema_signature(self.empty_url),
            schema_signature(self.reference_url),
        )

    def test_check_returns_zero_only_at_head(self):
        self.assertNotEqual(run_check(self.empty_url).returncode, 0)
        self.assertEqual(run_upgrade(self.empty_url).returncode, 0)
        self.assertEqual(run_check(self.empty_url).returncode, 0)

    def test_production_image_copies_alembic_configuration_and_scripts(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY alembic.ini ./", dockerfile)
        self.assertIn("COPY migrations ./migrations", dockerfile)
