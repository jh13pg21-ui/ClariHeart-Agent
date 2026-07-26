import importlib
import importlib.util
import unittest

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text


EXPECTED_COLUMNS = {
    "user_accounts": (
        ("id", "Integer", False),
        ("username", "String(64)", False),
        ("display_name", "String(128)", False),
        ("password_hash", "String(128)", False),
        ("roles_csv", "String(256)", False),
        ("created_at", "DateTime", False),
    ),
    "chat_sessions": (
        ("id", "Integer", False),
        ("public_id", "String(64)", False),
        ("title", "String(160)", False),
        ("user_id", "Integer", False),
        ("created_at", "DateTime", False),
        ("updated_at", "DateTime", False),
    ),
    "chat_messages": (
        ("id", "Integer", False),
        ("user_id", "Integer", False),
        ("session_id", "Integer", False),
        ("role", "String(32)", False),
        ("content", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "knowledge_chunks": (
        ("id", "Integer", False),
        ("source", "String(256)", False),
        ("source_index", "Integer", False),
        ("content", "Text", False),
        ("embedding_json", "Text", True),
        ("created_at", "DateTime", False),
    ),
    "psychological_reports": (
        ("id", "Integer", False),
        ("user_id", "Integer", False),
        ("session_id", "Integer", False),
        ("content", "Text", False),
        ("intent", "String(32)", False),
        ("emotion", "String(32)", False),
        ("emotion_score", "Float", False),
        ("risk_level", "String(32)", False),
        ("confidence", "Float", False),
        ("summary", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "risk_cases": (
        ("id", "Integer", False),
        ("report_id", "Integer", False),
        ("risk_level", "String(32)", False),
        ("status", "String(32)", False),
        ("owner", "String(128)", False),
        ("summary", "Text", False),
        ("handoff_summary", "Text", False),
        ("acknowledged_by", "String(128)", True),
        ("acknowledged_at", "DateTime", True),
        ("created_at", "DateTime", False),
        ("updated_at", "DateTime", False),
    ),
    "case_notes": (
        ("id", "Integer", False),
        ("case_id", "Integer", False),
        ("actor", "String(128)", False),
        ("note", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "alert_records": (
        ("id", "Integer", False),
        ("report_id", "Integer", False),
        ("channel", "String(64)", False),
        ("recipient", "String(256)", False),
        ("status", "String(32)", False),
        ("message", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "excel_records": (
        ("id", "Integer", False),
        ("report_id", "Integer", False),
        ("file_path", "String(512)", False),
        ("status", "String(32)", False),
        ("message", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "tool_jobs": (
        ("id", "Integer", False),
        ("report_id", "Integer", False),
        ("kind", "String(64)", False),
        ("status", "String(32)", False),
        ("attempts", "Integer", False),
        ("max_attempts", "Integer", False),
        ("depends_on_job_id", "Integer", True),
        ("run_after", "DateTime", False),
        ("last_error", "Text", False),
        ("created_at", "DateTime", False),
        ("updated_at", "DateTime", False),
    ),
    "dead_letter_records": (
        ("id", "Integer", False),
        ("job_id", "Integer", True),
        ("report_id", "Integer", False),
        ("kind", "String(64)", False),
        ("reason", "Text", False),
        ("payload", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "agent_run_traces": (
        ("id", "Integer", False),
        ("user_id", "Integer", False),
        ("session_id", "Integer", False),
        ("report_id", "Integer", True),
        ("intent", "String(32)", False),
        ("risk_level", "String(32)", False),
        ("original_input", "Text", False),
        ("sanitized_input", "Text", False),
        ("memory_brief", "Text", False),
        ("agent_steps_json", "Text", False),
        ("retrieved_knowledge_json", "Text", False),
        ("response_messages_json", "Text", False),
        ("assessment_json", "Text", False),
        ("created_at", "DateTime", False),
    ),
    "tool_audit_records": (
        ("id", "Integer", False),
        ("job_id", "Integer", True),
        ("report_id", "Integer", True),
        ("tool_name", "String(64)", False),
        ("policy", "String(128)", False),
        ("allowed", "Boolean", False),
        ("status", "String(32)", False),
        ("reason", "Text", False),
        ("payload", "Text", False),
        ("created_at", "DateTime", False),
        ("updated_at", "DateTime", False),
    ),
}

EXPECTED_FOREIGN_KEYS = {
    ("chat_sessions", "user_id", "user_accounts.id"),
    ("chat_messages", "user_id", "user_accounts.id"),
    ("chat_messages", "session_id", "chat_sessions.id"),
    ("psychological_reports", "user_id", "user_accounts.id"),
    ("psychological_reports", "session_id", "chat_sessions.id"),
    ("agent_run_traces", "user_id", "user_accounts.id"),
    ("agent_run_traces", "session_id", "chat_sessions.id"),
}

EXPECTED_INDEXES = {
    ("ix_user_accounts_username", "user_accounts", ("username",), True),
    ("ix_chat_sessions_public_id", "chat_sessions", ("public_id",), True),
    ("ix_knowledge_chunks_source", "knowledge_chunks", ("source",), False),
    ("ix_risk_cases_report_id", "risk_cases", ("report_id",), True),
    ("ix_risk_cases_risk_level", "risk_cases", ("risk_level",), False),
    ("ix_risk_cases_status", "risk_cases", ("status",), False),
    ("ix_case_notes_case_id", "case_notes", ("case_id",), False),
    ("ix_alert_records_report_id", "alert_records", ("report_id",), False),
    ("ix_excel_records_report_id", "excel_records", ("report_id",), False),
    ("ix_tool_jobs_report_id", "tool_jobs", ("report_id",), False),
    ("ix_tool_jobs_kind", "tool_jobs", ("kind",), False),
    ("ix_tool_jobs_status", "tool_jobs", ("status",), False),
    ("ix_tool_jobs_depends_on_job_id", "tool_jobs", ("depends_on_job_id",), False),
    ("ix_tool_jobs_run_after", "tool_jobs", ("run_after",), False),
    ("ix_dead_letter_records_job_id", "dead_letter_records", ("job_id",), False),
    ("ix_dead_letter_records_report_id", "dead_letter_records", ("report_id",), False),
    ("ix_dead_letter_records_kind", "dead_letter_records", ("kind",), False),
    ("ix_agent_run_traces_user_id", "agent_run_traces", ("user_id",), False),
    ("ix_agent_run_traces_session_id", "agent_run_traces", ("session_id",), False),
    ("ix_agent_run_traces_report_id", "agent_run_traces", ("report_id",), False),
    ("ix_agent_run_traces_intent", "agent_run_traces", ("intent",), False),
    ("ix_agent_run_traces_risk_level", "agent_run_traces", ("risk_level",), False),
    ("ix_tool_audit_records_job_id", "tool_audit_records", ("job_id",), False),
    ("ix_tool_audit_records_report_id", "tool_audit_records", ("report_id",), False),
    ("ix_tool_audit_records_tool_name", "tool_audit_records", ("tool_name",), False),
    ("ix_tool_audit_records_status", "tool_audit_records", ("status",), False),
}

EXPECTED_CLIENT_DEFAULTS = {
    ("user_accounts", "roles_csv"): "ROLE_USER",
    ("user_accounts", "created_at"): "<callable>",
    ("chat_sessions", "created_at"): "<callable>",
    ("chat_sessions", "updated_at"): "<callable>",
    ("chat_messages", "created_at"): "<callable>",
    ("knowledge_chunks", "created_at"): "<callable>",
    ("psychological_reports", "created_at"): "<callable>",
    ("risk_cases", "owner"): "unassigned",
    ("risk_cases", "handoff_summary"): "",
    ("risk_cases", "created_at"): "<callable>",
    ("risk_cases", "updated_at"): "<callable>",
    ("case_notes", "created_at"): "<callable>",
    ("alert_records", "created_at"): "<callable>",
    ("excel_records", "created_at"): "<callable>",
    ("tool_jobs", "attempts"): 0,
    ("tool_jobs", "max_attempts"): 3,
    ("tool_jobs", "run_after"): "<callable>",
    ("tool_jobs", "last_error"): "",
    ("tool_jobs", "created_at"): "<callable>",
    ("tool_jobs", "updated_at"): "<callable>",
    ("dead_letter_records", "payload"): "",
    ("dead_letter_records", "created_at"): "<callable>",
    ("agent_run_traces", "risk_level"): "LOW",
    ("agent_run_traces", "memory_brief"): "",
    ("agent_run_traces", "agent_steps_json"): "[]",
    ("agent_run_traces", "retrieved_knowledge_json"): "[]",
    ("agent_run_traces", "response_messages_json"): "[]",
    ("agent_run_traces", "assessment_json"): "{}",
    ("agent_run_traces", "created_at"): "<callable>",
    ("tool_audit_records", "policy"): "",
    ("tool_audit_records", "allowed"): True,
    ("tool_audit_records", "reason"): "",
    ("tool_audit_records", "payload"): "{}",
    ("tool_audit_records", "created_at"): "<callable>",
    ("tool_audit_records", "updated_at"): "<callable>",
}


def type_token(column_type) -> str:
    if isinstance(column_type, Text):
        return "Text"
    if isinstance(column_type, String):
        return f"String({column_type.length})"
    if isinstance(column_type, Integer):
        return "Integer"
    if isinstance(column_type, DateTime):
        return "DateTime"
    if isinstance(column_type, Float):
        return "Float"
    if isinstance(column_type, Boolean):
        return "Boolean"
    raise AssertionError(f"未冻结的历史列类型：{column_type!r}")


def client_default_token(column):
    if column.default is None:
        return None
    if column.default.is_callable:
        return "<callable>"
    return column.default.arg


class LegacySchemaContractTests(unittest.TestCase):
    def test_frozen_metadata_matches_3a3e283_historical_orm_contract(self):
        spec = importlib.util.find_spec("migrations.legacy_schema")
        self.assertIsNotNone(spec, "需要新增不依赖 0001 的 migrations.legacy_schema 权威基线")
        metadata = importlib.import_module("migrations.legacy_schema").LEGACY_METADATA

        actual_columns = {
            table.name: tuple(
                (column.name, type_token(column.type), column.nullable)
                for column in table.columns
            )
            for table in metadata.tables.values()
        }
        self.assertEqual(actual_columns, EXPECTED_COLUMNS)

        actual_primary_keys = {
            table.name: tuple(table.primary_key.columns.keys())
            for table in metadata.tables.values()
        }
        self.assertEqual(
            actual_primary_keys,
            {table_name: ("id",) for table_name in EXPECTED_COLUMNS},
        )

        actual_foreign_keys = {
            (table.name, foreign_key.parent.name, foreign_key.target_fullname)
            for table in metadata.tables.values()
            for foreign_key in table.foreign_keys
        }
        self.assertEqual(actual_foreign_keys, EXPECTED_FOREIGN_KEYS)

        actual_indexes = {
            (index.name, table.name, tuple(index.columns.keys()), bool(index.unique))
            for table in metadata.tables.values()
            for index in table.indexes
        }
        self.assertEqual(actual_indexes, EXPECTED_INDEXES)

        unique_constraints = {
            (constraint.name, table.name, tuple(constraint.columns.keys()))
            for table in metadata.tables.values()
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        self.assertEqual(unique_constraints, set())

        actual_client_defaults = {
            (table.name, column.name): client_default_token(column)
            for table in metadata.tables.values()
            for column in table.columns
            if column.default is not None
        }
        self.assertEqual(actual_client_defaults, EXPECTED_CLIENT_DEFAULTS)

        server_default_columns = {
            (table.name, column.name)
            for table in metadata.tables.values()
            for column in table.columns
            if column.server_default is not None
        }
        self.assertEqual(server_default_columns, set())
