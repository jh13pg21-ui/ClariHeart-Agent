import os
import subprocess
import sys
import unittest

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.cli.migrate import BASELINE_REVISION, alembic_config


MYSQL_URL = os.environ.get("MINDBRIDGE_TEST_MYSQL_URL")


@unittest.skipUnless(MYSQL_URL, "需要显式设置 MINDBRIDGE_TEST_MYSQL_URL 指向隔离 MySQL")
class MySqlMigrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(MYSQL_URL)
        with self.engine.begin() as connection:
            connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for table in inspect(self.engine).get_table_names():
                connection.execute(text(f"DROP TABLE `{table}`"))
            connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    def tearDown(self):
        self.engine.dispose()

    def _legacy_baseline(self):
        command.upgrade(alembic_config(MYSQL_URL), BASELINE_REVISION)
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE alembic_version"))

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
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "0002_auth_audit_outbox_schema")
            self.assertEqual(connection.execute(text("SELECT password_hash FROM user_accounts WHERE id=1")).scalar_one(), "legacy-hash")
            self.assertEqual(connection.execute(text("SELECT content FROM chat_messages WHERE id=1")).scalar_one(), "preserve-me")

    def test_mysql_upgrade_is_idempotent(self):
        self._legacy_baseline()
        self.assertEqual(self._upgrade().returncode, 0)
        result = self._upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar_one(), 1)
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "0002_auth_audit_outbox_schema")

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
