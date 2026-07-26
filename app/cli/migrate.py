"""安全的 Alembic 迁移入口。"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
import sqlalchemy as sa

from app.core.config import get_settings


BASELINE_REVISION = "0001_existing_schema_baseline"
LEGACY_COLUMNS = {
    "user_accounts": {"id", "username", "display_name", "password_hash", "roles_csv", "created_at"},
    "chat_sessions": {"id", "public_id", "title", "user_id", "created_at", "updated_at"},
    "chat_messages": {"id", "user_id", "session_id", "role", "content", "created_at"},
    "knowledge_chunks": {"id", "source", "source_index", "content", "embedding_json", "created_at"},
    "psychological_reports": {"id", "user_id", "session_id", "content", "intent", "emotion", "emotion_score", "risk_level", "confidence", "summary", "created_at"},
    "risk_cases": {"id", "report_id", "risk_level", "status", "owner", "summary", "handoff_summary", "acknowledged_by", "acknowledged_at", "created_at", "updated_at"},
    "case_notes": {"id", "case_id", "actor", "note", "created_at"},
    "alert_records": {"id", "report_id", "channel", "recipient", "status", "message", "created_at"},
    "excel_records": {"id", "report_id", "file_path", "status", "message", "created_at"},
    "tool_jobs": {"id", "report_id", "kind", "status", "attempts", "max_attempts", "depends_on_job_id", "run_after", "last_error", "created_at", "updated_at"},
    "dead_letter_records": {"id", "job_id", "report_id", "kind", "reason", "payload", "created_at"},
    "agent_run_traces": {"id", "user_id", "session_id", "report_id", "intent", "risk_level", "original_input", "sanitized_input", "memory_brief", "agent_steps_json", "retrieved_knowledge_json", "response_messages_json", "assessment_json", "created_at"},
    "tool_audit_records": {"id", "job_id", "report_id", "tool_name", "policy", "allowed", "status", "reason", "payload", "created_at", "updated_at"},
}


class MigrationSafetyError(RuntimeError):
    """未版本化数据库无法证明可安全迁移时抛出。"""


def alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def legacy_schema_matches(database_url: str) -> bool:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if tables != set(LEGACY_COLUMNS):
            return False
        if engine.dialect.name != "sqlite":
            return _schema_signature(inspector) == _baseline_signature(engine.dialect)
        with tempfile.TemporaryDirectory() as directory:
            reference_url = f"sqlite:///{Path(directory) / 'baseline.db'}"
            command.upgrade(alembic_config(reference_url), BASELINE_REVISION)
            reference_engine = create_engine(reference_url)
            try:
                return _schema_signature(inspector) == _schema_signature(inspect(reference_engine))
            finally:
                reference_engine.dispose()
    finally:
        engine.dispose()


def _schema_signature(inspector) -> dict:
    return {
        table: {
            "columns": tuple((column["name"], str(column["type"]), column["nullable"], _normalize_default(column["default"]), column["primary_key"]) for column in inspector.get_columns(table)),
            "foreign_keys": tuple(sorted((foreign_key["constrained_columns"], foreign_key["referred_table"], foreign_key["referred_columns"]) for foreign_key in inspector.get_foreign_keys(table))),
            "unique": tuple(sorted(tuple(item["column_names"]) for item in inspector.get_unique_constraints(table))),
            "indexes": tuple(sorted((tuple(item["column_names"]), item["unique"]) for item in inspector.get_indexes(table))),
        }
        for table in sorted(table for table in inspector.get_table_names() if table != "alembic_version")
    }


def _baseline_signature(dialect) -> dict:
    """从 0001 脚本捕获基线定义，不在目标数据库创建任何比较对象。"""
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0001_existing_schema_baseline.py"
    spec = importlib.util.spec_from_file_location("mindbridge_legacy_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    metadata = sa.MetaData()

    class Recorder:
        def create_table(self, name, *items):
            sa.Table(name, metadata, *items)

    module.op = Recorder()
    module.upgrade()
    return {
        table.name: {
            "columns": tuple((column.name, str(column.type.compile(dialect=dialect)), column.nullable, _default_text(column.server_default), column.primary_key) for column in table.columns),
            "foreign_keys": tuple(sorted(([foreign_key.parent.name], foreign_key.column.table.name, [foreign_key.column.name]) for foreign_key in table.foreign_keys)),
            "unique": tuple(sorted(tuple(constraint.columns.keys()) for constraint in table.constraints if isinstance(constraint, sa.UniqueConstraint))),
            "indexes": tuple(sorted((tuple(index.columns.keys()), index.unique) for index in table.indexes)),
        }
        for table in metadata.tables.values()
    }


def _default_text(default) -> str | None:
    if default is None:
        return None
    return str(default.arg).strip("'\"")


def _normalize_default(value) -> str | None:
    if value is None:
        return None
    return str(value).strip().strip("()'\\"")


def database_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        if "alembic_version" not in inspector.get_table_names():
            return None
        with engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    finally:
        engine.dispose()


def upgrade(database_url: str) -> None:
    config = alembic_config(database_url)
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    if "alembic_version" not in tables and tables:
        if not legacy_schema_matches(database_url):
            raise MigrationSafetyError("unversioned database does not match the known legacy baseline; refusing migration")
        command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")


def check(database_url: str) -> None:
    revision = database_revision(database_url)
    head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
    if revision != head:
        raise MigrationSafetyError(f"database revision is {revision or 'unversioned'}, expected {head}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage MindBridge database migrations")
    parser.add_argument("command", choices=("check", "upgrade"))
    arguments = parser.parse_args(argv)
    database_url = get_settings().database_url
    try:
        if arguments.command == "upgrade":
            upgrade(database_url)
        else:
            check(database_url)
    except Exception as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
