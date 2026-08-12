from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import ToolJobKind
from app.services.tool_commands import ToolCommandService
from app.services.tools import ToolOrchestrationService

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先安装 requirements.txt 中的 mcp 依赖") from exc


mcp = FastMCP("mindbridge-python-tools")


@mcp.tool()
def mindbridge_excel_report(report_id: int) -> str:
    """Queue one psychological report for reliable Excel ledger delivery."""
    db = SessionLocal()
    try:
        event = ToolCommandService(db, get_settings()).enqueue_report_command(report_id, ToolJobKind.EXCEL_REPORT.value)
        return f"queued: eventId={event.event_id}, reportId={report_id}"
    except (ValueError, PermissionError) as exc:
        return str(exc)
    finally:
        db.close()


@mcp.tool()
def mindbridge_case_create(report_id: int) -> str:
    """Create or return the active MindBridge risk case for one psychological report."""
    db = SessionLocal()
    try:
        event = ToolCommandService(db, get_settings()).enqueue_report_command(report_id, ToolJobKind.CASE_CREATE.value)
        return f"queued: eventId={event.event_id}, reportId={report_id}"
    except (ValueError, PermissionError) as exc:
        return str(exc)
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_send(case_id: int) -> str:
    """Send or record the counselor alert for one MindBridge risk case."""
    db = SessionLocal()
    try:
        from app.models.entities import RiskCase

        case = db.get(RiskCase, case_id)
        if case is None:
            return f"case {case_id} not found"
        event = ToolCommandService(db, get_settings()).enqueue_case_alert(case_id)
        return f"queued: eventId={event.event_id}, caseId={case_id}"
    except (ValueError, PermissionError) as exc:
        return str(exc)
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_ack(case_id: int, actor: str, note: str = "") -> str:
    """Mark a MindBridge risk case as acknowledged by a counselor or administrator."""
    db = SessionLocal()
    try:
        settings = get_settings()
        safe_actor = ToolCommandService(db, settings).require_mcp_actor(actor)
        case = ToolOrchestrationService(db, settings).acknowledge_case(case_id, safe_actor, note)
        return f"success: caseId={case.id}, status={case.status}, acknowledgedBy={case.acknowledged_by}"
    except (RuntimeError, PermissionError) as exc:
        return str(exc)
    finally:
        db.close()


@mcp.tool()
def mindbridge_case_note_add(case_id: int, actor: str, note: str) -> str:
    """Append a follow-up note to a MindBridge risk case."""
    db = SessionLocal()
    try:
        settings = get_settings()
        safe_actor = ToolCommandService(db, settings).require_mcp_actor(actor)
        record = ToolOrchestrationService(db, settings).add_case_note(case_id, safe_actor, note)
        return f"success: noteId={record.id}, caseId={record.case_id}"
    except (RuntimeError, PermissionError) as exc:
        return str(exc)
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_notify(report_id: int) -> str:
    """Send a high-risk alert email and record the notification result for one psychological report."""
    db = SessionLocal()
    try:
        event = ToolCommandService(db, get_settings()).enqueue_report_command(report_id, ToolJobKind.ALERT_SEND.value)
        return f"queued: eventId={event.event_id}, reportId={report_id}"
    except (ValueError, PermissionError) as exc:
        return str(exc)
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
