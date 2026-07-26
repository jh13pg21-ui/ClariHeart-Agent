"""安全的 Alembic 迁移入口。"""
from __future__ import annotations

import argparse
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
from migrations.legacy_schema import LEGACY_METADATA


BASELINE_REVISION = "0001_existing_schema_baseline"
LEGACY_COLUMNS = {
    table.name: set(table.columns.keys())
    for table in LEGACY_METADATA.tables.values()
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
            baseline = LEGACY_METADATA
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
            reference_engine = create_engine(reference_url)
            try:
                LEGACY_METADATA.create_all(reference_engine)
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
            "foreign_keys": tuple(
                sorted(
                    (
                        tuple(foreign_key["constrained_columns"]),
                        foreign_key["referred_table"],
                        tuple(foreign_key["referred_columns"]),
                        _foreign_key_options_signature(
                            foreign_key.get("options") or {}
                        ),
                    )
                    for foreign_key in inspector.get_foreign_keys(table)
                )
            ),
            "unique": tuple(
                sorted(
                    (
                        item.get("name") or "",
                        tuple(item["column_names"]),
                    )
                    for item in inspector.get_unique_constraints(table)
                )
            ),
            "indexes": tuple(
                sorted(
                    (
                        item.get("name") or "",
                        tuple(item["column_names"]),
                        bool(item["unique"]),
                    )
                    for item in inspector.get_indexes(table)
                )
            ),
        }
    return signature


def _foreign_key_options_signature(options: dict) -> tuple:
    return tuple(
        sorted(
            (
                str(key).lower(),
                value.upper() if isinstance(value, str) else value,
            )
            for key, value in options.items()
            if value is not None
        )
    )


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
