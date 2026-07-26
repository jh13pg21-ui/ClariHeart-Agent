"""安全的 Alembic 迁移入口。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings


BASELINE_REVISION = "0001_existing_schema_baseline"
HEAD_REVISION = "0002_auth_audit_outbox_schema"
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
        return all({column["name"] for column in inspector.get_columns(name)} == columns for name, columns in LEGACY_COLUMNS.items())
    finally:
        engine.dispose()


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
    inspector = inspect(create_engine(database_url))
    tables = set(inspector.get_table_names())
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
