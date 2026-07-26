"""安全的 Alembic 迁移入口。"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
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
            baseline = _baseline_metadata()
            if not _schema_layout_matches(inspector, baseline):
                return False
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection,
                    opts={
                        "compare_type": True,
                        "compare_server_default": _server_default_differs,
                        "target_metadata": baseline,
                    },
                )
                return not compare_metadata(context, baseline)
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
    signature = {}
    for table in sorted(table for table in inspector.get_table_names() if table != "alembic_version"):
        primary_keys = set(inspector.get_pk_constraint(table).get("constrained_columns") or ())
        signature[table] = {
            "columns": tuple((column["name"], str(column["type"]), column["nullable"], _normalize_default(column["default"]), column["name"] in primary_keys) for column in inspector.get_columns(table)),
            "foreign_keys": tuple(sorted((foreign_key["constrained_columns"], foreign_key["referred_table"], foreign_key["referred_columns"]) for foreign_key in inspector.get_foreign_keys(table))),
            "unique": tuple(sorted(tuple(item["column_names"]) for item in inspector.get_unique_constraints(table))),
            "indexes": tuple(sorted((tuple(item["column_names"]), bool(item["unique"])) for item in inspector.get_indexes(table))),
        }
    return signature


def _baseline_metadata() -> sa.MetaData:
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
    return metadata


def _schema_layout_matches(inspector, metadata: sa.MetaData) -> bool:
    for table_name, table in metadata.tables.items():
        if tuple(column["name"] for column in inspector.get_columns(table_name)) != tuple(table.columns.keys()):
            return False
        actual_primary_key = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
        if actual_primary_key != tuple(table.primary_key.columns.keys()):
            return False
    return True


def _server_default_differs(
    context,
    inspected_column,
    metadata_column,
    inspected_default,
    metadata_default,
    rendered_metadata_default,
) -> bool:
    return _normalize_server_default(inspected_default, metadata_column.type) != _normalize_server_default(
        rendered_metadata_default, metadata_column.type
    )


def _normalize_server_default(value, column_type) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    while len(normalized) >= 2 and normalized[0] == "(" and normalized[-1] == ")":
        normalized = normalized[1:-1].strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "'\"":
        normalized = normalized[1:-1]
    if isinstance(column_type, sa.Boolean):
        return {
            "1": "true",
            "true": "true",
            "0": "false",
            "false": "false",
        }.get(normalized.lower(), normalized.lower())
    return normalized


def _normalize_default(value) -> str | None:
    if value is None:
        return None
    return str(value).strip().strip("()'")


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
