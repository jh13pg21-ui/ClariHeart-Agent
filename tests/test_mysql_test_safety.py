import importlib
import importlib.util
import unittest


class MySqlTestSafetyTests(unittest.TestCase):
    def _safety_module(self):
        spec = importlib.util.find_spec("tests.mysql_test_safety")
        self.assertIsNotNone(spec, "MySQL destructive 测试需要程序化防误删护栏")
        return importlib.import_module("tests.mysql_test_safety")

    def test_production_style_database_name_is_rejected(self):
        safety = self._safety_module()
        with self.assertRaisesRegex(RuntimeError, "mindbridge_test_"):
            safety.validate_destructive_database_target(
                "mysql+pymysql://tester:secret@mysql/mindbridge",
                "mindbridge",
                "1",
            )

    def test_explicit_confirmation_is_required(self):
        safety = self._safety_module()
        with self.assertRaisesRegex(
            RuntimeError,
            "MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS=1",
        ):
            safety.validate_destructive_database_target(
                "mysql+pymysql://tester:secret@mysql/mindbridge_test_migrations",
                "mindbridge_test_migrations",
                None,
            )

    def test_url_database_must_equal_selected_database(self):
        safety = self._safety_module()
        with self.assertRaisesRegex(RuntimeError, "URL.*SELECT DATABASE"):
            safety.validate_destructive_database_target(
                "mysql+pymysql://tester:secret@mysql/mindbridge_test_expected",
                "mindbridge_test_other",
                "1",
            )

    def test_matching_test_database_with_confirmation_is_allowed(self):
        safety = self._safety_module()
        selected = safety.validate_destructive_database_target(
            "mysql+pymysql://tester:secret@mysql/mindbridge_test_migrations",
            "mindbridge_test_migrations",
            "1",
        )
        self.assertEqual(selected, "mindbridge_test_migrations")

    def test_unsafe_database_is_rejected_before_transaction_or_ddl(self):
        safety = self._safety_module()
        self.assertTrue(
            hasattr(safety, "reset_mysql_schema"),
            "护栏必须封装 destructive reset，保证验证发生在 begin/DROP 之前",
        )

        class ScalarResult:
            def scalar_one_or_none(self):
                return "mindbridge"

        class Connection:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def execute(self, statement):
                self.statements.append(str(statement))
                return ScalarResult()

        class Engine:
            def __init__(self):
                self.connection = Connection()
                self.transaction_started = False

            def connect(self):
                return self.connection

            def begin(self):
                self.transaction_started = True
                raise AssertionError("不安全数据库不得启动 destructive 事务")

        engine = Engine()
        with self.assertRaisesRegex(RuntimeError, "mindbridge_test_"):
            safety.reset_mysql_schema(
                engine,
                "mysql+pymysql://tester:secret@mysql/mindbridge",
                "1",
            )
        self.assertEqual(engine.connection.statements, ["SELECT DATABASE()"])
        self.assertFalse(engine.transaction_started)
