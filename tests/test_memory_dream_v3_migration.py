import tempfile
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.cli.migrate import alembic_config


class MemoryDreamV3MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.url = f"sqlite:///{Path(self.tempdir.name) / 'memory-dream-v3.db'}"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_upgrade_adds_memory_lineage_and_per_user_dream_state(self):
        command.upgrade(alembic_config(self.url), "head")
        engine = create_engine(self.url)
        try:
            inspector = inspect(engine)
            memory_columns = {
                column["name"]
                for column in inspector.get_columns("long_term_memories")
            }
            state_columns = {
                column["name"]
                for column in inspector.get_columns("memory_dream_states")
            }
            state_uniques = {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints("memory_dream_states")
            }
        finally:
            engine.dispose()

        self.assertTrue(
            {
                "memory_key",
                "conflict_group_id",
                "consolidated_from_ids_json",
                "resolution_reason",
            }
            <= memory_columns
        )
        self.assertTrue(
            {
                "user_id",
                "last_scanned_at",
                "last_consolidated_at",
                "lease_owner",
                "lease_acquired_at",
                "lease_expires_at",
                "last_error",
            }
            <= state_columns
        )
        self.assertIn(("user_id",), state_uniques)

    def test_upgrade_backfills_stable_memory_key_without_losing_history(self):
        config = alembic_config(self.url)
        command.upgrade(config, "0010_memory_v2_traces")
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
                        "(id, public_id, user_id, source_session_id, memory_type, name, description, "
                        "body, content_hash, created_at, updated_at, last_accessed_at, status, "
                        "evidence_message_ids_json, confidence, extraction_method, prompt_version, "
                        "model_provider, model_name, version, usage_count, confirmation_count) "
                        "VALUES (1, 'memory-1', 1, NULL, 'PREFERENCE', '回复风格', '偏好', "
                        "'先给结论', 'hash-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, 'ACTIVE', "
                        "'[]', 0.5, 'legacy', '', '', '', 1, 0, 0)"
                    )
                )

            command.upgrade(config, "head")
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT memory_key, consolidated_from_ids_json, resolution_reason, status "
                        "FROM long_term_memories WHERE id = 1"
                    )
                ).one()
        finally:
            engine.dispose()

        self.assertEqual(row.memory_key, "preference.回复风格")
        self.assertEqual(row.consolidated_from_ids_json, "[]")
        self.assertEqual(row.resolution_reason, "")
        self.assertEqual(row.status, "ACTIVE")

    def test_current_orm_exposes_memory_dream_state(self):
        from app.models.entities import LongTermMemory, MemoryDreamState

        self.assertIn("memory_key", LongTermMemory.__table__.columns)
        self.assertEqual(MemoryDreamState.__tablename__, "memory_dream_states")
        self.assertTrue(
            any(
                constraint.name == "uq_memory_dream_states_user_id"
                for constraint in MemoryDreamState.__table__.constraints
            )
        )
