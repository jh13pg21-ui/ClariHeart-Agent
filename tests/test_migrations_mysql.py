import os
import subprocess
import sys
import unittest

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.cli.migrate import _server_default_differs
from migrations.legacy_schema import LEGACY_METADATA
from tests.mysql_test_safety import reset_mysql_schema


MYSQL_URL = os.environ.get("MINDBRIDGE_TEST_MYSQL_URL")
ALLOW_DESTRUCTIVE = os.environ.get("MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS")


@unittest.skipUnless(
    MYSQL_URL and ALLOW_DESTRUCTIVE == "1",
    "需要隔离 MySQL URL 及 MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS=1 双重确认",
)
class MySqlMigrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(MYSQL_URL)
        try:
            reset_mysql_schema(self.engine, MYSQL_URL, ALLOW_DESTRUCTIVE)
        except Exception:
            self.engine.dispose()
            raise

    def tearDown(self):
        self.engine.dispose()

    def _legacy_baseline(self):
        LEGACY_METADATA.create_all(self.engine)

    def _upgrade(self):
        environment = os.environ.copy()
        environment["DATABASE_URL"] = MYSQL_URL
        return subprocess.run([sys.executable, "-m", "app.cli.migrate", "upgrade"], text=True, capture_output=True, env=environment)

    def _assert_rejected_without_version_table(self):
        result = self._upgrade()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("alembic_version", inspect(self.engine).get_table_names())

    def test_exact_legacy_mysql_is_stamped_and_preserves_rows(self):
        self._legacy_baseline()
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO user_accounts (id, username, display_name, password_hash, roles_csv, created_at) VALUES (1, 'legacy', 'Legacy', 'legacy-hash', 'ROLE_USER', NOW())"))
            connection.execute(text("INSERT INTO chat_sessions (id, public_id, title, user_id, created_at, updated_at) VALUES (1, 'legacy-session', 'Legacy', 1, NOW(), NOW())"))
            connection.execute(text("INSERT INTO chat_messages (id, user_id, session_id, role, content, created_at) VALUES (1, 1, 1, 'user', 'preserve-me', NOW())"))
        result = self._upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "0007_structured_summary")
            self.assertEqual(connection.execute(text("SELECT password_hash FROM user_accounts WHERE id=1")).scalar_one(), "legacy-hash")
            self.assertEqual(connection.execute(text("SELECT content FROM chat_messages WHERE id=1")).scalar_one(), "preserve-me")

    def test_mysql_upgrade_is_idempotent(self):
        self._legacy_baseline()
        self.assertEqual(self._upgrade().returncode, 0)
        result = self._upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one(), 1)
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "0007_structured_summary")

    def test_mysql_type_length_drift_is_rejected_without_version_table(self):
        self._legacy_baseline()
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE user_accounts MODIFY username VARCHAR(63) NOT NULL"))
        self._assert_rejected_without_version_table()

    def test_mysql_foreign_key_drift_is_rejected_without_version_table(self):
        self._legacy_baseline()
        foreign_key = next(
            item
            for item in inspect(self.engine).get_foreign_keys("chat_sessions")
            if item["constrained_columns"] == ["user_id"]
        )
        with self.engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE chat_sessions DROP FOREIGN KEY `{foreign_key['name']}`"))
        self._assert_rejected_without_version_table()

    def test_mysql_index_drift_is_rejected_without_version_table(self):
        self._legacy_baseline()
        with self.engine.begin() as connection:
            connection.execute(text("CREATE INDEX ix_unexpected_roles_csv ON user_accounts (roles_csv)"))
        self._assert_rejected_without_version_table()

    def test_mysql_server_default_drift_is_rejected_without_version_table(self):
        self._legacy_baseline()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE user_accounts "
                    "ALTER COLUMN roles_csv SET DEFAULT 'ROLE_USER'"
                )
            )
        self._assert_rejected_without_version_table()

    def test_mysql_head_matches_current_orm_schema(self):
        result = self._upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)

        from app.core.database import Base
        import app.models.entities  # noqa: F401

        inspector = inspect(self.engine)
        for table_name, table in Base.metadata.tables.items():
            actual_columns = tuple(
                column["name"] for column in inspector.get_columns(table_name)
            )
            self.assertEqual(actual_columns, tuple(table.columns.keys()))
            actual_primary_key = tuple(
                inspector.get_pk_constraint(table_name).get(
                    "constrained_columns"
                )
                or ()
            )
            self.assertEqual(
                actual_primary_key,
                tuple(table.primary_key.columns.keys()),
            )

        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": _server_default_differs,
                    "target_metadata": Base.metadata,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])
