import tempfile
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.cli.migrate import alembic_config


class MemoryV2MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self.tempdir.name) / 'memory-v2.db'}"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_existing_memory_is_backfilled_as_active_legacy_memory(self):
        config = alembic_config(self.url)
        command.upgrade(config, "0009_context_compaction")
        engine = create_engine(self.url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO user_accounts "
                        "(id, username, display_name, password_hash, roles_csv, created_at, "
                        "password_algorithm, must_reset_password, disabled, long_term_memory_enabled) "
                        "VALUES (1, 'student', 'Student', 'hash', 'ROLE_USER', CURRENT_TIMESTAMP, "
                        "'legacy_sha256', 1, 0, 1)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO long_term_memories "
                        "(id, public_id, user_id, source_session_id, memory_type, name, "
                        "description, body, content_hash, created_at, updated_at, last_accessed_at) "
                        "VALUES (1, 'memory-1', 1, NULL, 'PREFERENCE', '回复风格', "
                        "'偏好', '先给结论', 'hash-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)"
                    )
                )
            command.upgrade(config, "head")
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT status, confidence, extraction_method, evidence_message_ids_json, "
                        "version, usage_count, confirmation_count "
                        "FROM long_term_memories WHERE id = 1"
                    )
                ).one()
            self.assertEqual(row.status, "ACTIVE")
            self.assertAlmostEqual(row.confidence, 0.5)
            self.assertEqual(row.extraction_method, "legacy")
            self.assertEqual(row.evidence_message_ids_json, "[]")
            self.assertEqual(row.version, 1)
            self.assertEqual(row.usage_count, 0)
            self.assertEqual(row.confirmation_count, 0)
        finally:
            engine.dispose()

    def test_model_trace_schema_is_metadata_only(self):
        command.upgrade(alembic_config(self.url), "head")
        engine = create_engine(self.url)
        try:
            columns = {
                column["name"]
                for column in inspect(engine).get_columns("model_call_traces")
            }
        finally:
            engine.dispose()

        self.assertFalse(
            columns.intersection(
                {"prompt", "messages", "content", "input_text", "output_text"}
            )
        )
        self.assertTrue(
            {
                "prompt_manifest_hash",
                "context_plan_hash",
                "input_tokens",
                "output_tokens",
                "cloud_egress",
            }
            <= columns
        )

    def test_memory_v2_composite_indexes_exist(self):
        command.upgrade(alembic_config(self.url), "head")
        engine = create_engine(self.url)
        try:
            inspector = inspect(engine)
            memory_indexes = {
                tuple(index["column_names"])
                for index in inspector.get_indexes("long_term_memories")
            }
            run_indexes = {
                tuple(index["column_names"])
                for index in inspector.get_indexes("memory_consolidation_runs")
            }
        finally:
            engine.dispose()

        self.assertIn(("user_id", "status", "updated_at"), memory_indexes)
        self.assertIn(("user_id", "status", "created_at"), run_indexes)
