from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import ToolJobKind
from app.models.entities import OutboxEvent, PsychologicalReport, RiskCase, ToolAuditRecord
from app.services.outbox import OutboxService
from app.services.tool_governance import ToolPolicyRegistry


REPORT_COMMANDS = {
    ToolJobKind.EXCEL_REPORT.value: "report.excel",
    ToolJobKind.CASE_CREATE.value: "case.create",
    ToolJobKind.ALERT_SEND.value: "case.create",
}


class ToolCommandService:
    """所有 HTTP、MCP 和 Agent 入口共享的可靠工具命令入口。"""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def enqueue_report_command(self, report_id: int, kind: str) -> OutboxEvent:
        report = self.db.get(PsychologicalReport, report_id)
        if report is None:
            raise ValueError(f"report {report_id} not found")
        event_type = REPORT_COMMANDS.get(kind)
        if event_type is None:
            raise ValueError(f"unsupported report command: {kind}")
        policy_kind = ToolJobKind.CASE_CREATE.value if kind == ToolJobKind.ALERT_SEND.value else kind
        self._require_allowed(policy_kind, report)
        event = OutboxService.add_event(
            self.db,
            event_type,
            "report",
            report.id,
            {"reportId": report.id, "riskLevel": report.risk_level},
            f"{event_type}:{report.id}",
        )
        self._audit_queued(policy_kind, report, event)
        self.db.commit()
        return event

    def enqueue_case_alert(self, case_id: int) -> OutboxEvent:
        case = self.db.get(RiskCase, case_id)
        if case is None:
            raise ValueError(f"case {case_id} not found")
        report = self.db.get(PsychologicalReport, case.report_id)
        self._require_allowed(ToolJobKind.ALERT_SEND.value, report)
        event = OutboxService.add_event(
            self.db,
            "case.created",
            "case",
            case.id,
            {"reportId": report.id, "riskLevel": report.risk_level},
            f"case.created:{case.id}",
        )
        self._audit_queued(ToolJobKind.ALERT_SEND.value, report, event)
        self.db.commit()
        return event

    def require_mcp_actor(self, actor: str) -> str:
        normalized = actor.strip().lower()
        allowed = {
            item.strip().lower()
            for item in str(getattr(self.settings, "mcp_allowed_actors", "")).split(",")
            if item.strip()
        }
        if not normalized or normalized not in allowed:
            raise PermissionError("MCP actor 未被授权执行人工个案操作")
        return normalized

    def _require_allowed(self, kind: str, report: PsychologicalReport | None) -> None:
        allowed, reason, _ = ToolPolicyRegistry.authorize(kind, report)
        if not allowed:
            self.db.add(
                ToolAuditRecord(
                    job_id=None,
                    report_id=report.id if report is not None else None,
                    tool_name=kind,
                    policy=kind,
                    allowed=False,
                    status="BLOCKED",
                    reason=reason,
                    payload="{}",
                )
            )
            self.db.commit()
            raise PermissionError(reason)

    def _audit_queued(self, kind: str, report: PsychologicalReport, event: OutboxEvent) -> None:
        self.db.add(
            ToolAuditRecord(
                job_id=None,
                report_id=report.id,
                tool_name=kind,
                policy=kind,
                allowed=True,
                status="QUEUED",
                reason="命令已通过统一治理入口写入 Transactional Outbox",
                payload=json.dumps(
                    {"eventId": event.event_id, "eventType": event.event_type},
                    ensure_ascii=False,
                ),
            )
        )
