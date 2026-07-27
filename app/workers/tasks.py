from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import (
    ProcessedMessage,
    PsychologicalReport,
    RiskCase,
    ToolJob,
)
from app.services.outbox import OutboxService
from app.services.tool_governance import ToolGovernanceService
from app.services.tools import ToolOrchestrationService
from app.workers.celery_app import celery_app


worker_settings = get_settings()


class RetryableTaskError(RuntimeError):
    pass


def _claim_message(
    db: Session,
    consumer: str,
    event_id: str,
) -> ProcessedMessage | None:
    marker = ProcessedMessage(consumer=consumer, message_id=event_id)
    db.add(marker)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return None
    return marker


def _start_job(db: Session, report_id: int, kind: str) -> ToolJob:
    job = ToolJob(
        report_id=report_id,
        kind=kind,
        status=ToolJobStatus.RUNNING.value,
        attempts=1,
        max_attempts=5,
        run_after=datetime.utcnow(),
        last_error="",
    )
    db.add(job)
    db.flush()
    return job


def _run_governed(
    consumer: str,
    event_id: str,
    report_id: int,
    kind: str,
    operation: Callable[[Session, PsychologicalReport, ToolJob], dict],
) -> dict:
    db: Session = SessionLocal()
    try:
        marker = _claim_message(db, consumer, event_id)
        if marker is None:
            return {"status": "ALREADY_PROCESSED", "eventId": event_id}
        report = db.get(PsychologicalReport, report_id)
        if report is None:
            raise ValueError(f"report {report_id} not found")

        job = _start_job(db, report_id, kind)
        governance = ToolGovernanceService(db)
        audit = governance.start_job(job, report)
        if not audit.allowed:
            job.status = ToolJobStatus.DEAD.value
            job.last_error = audit.reason
            job.updated_at = datetime.utcnow()
            governance.finish(audit, "BLOCKED", audit.reason)
            db.commit()
            return {"status": "BLOCKED", "eventId": event_id, "reason": audit.reason}

        governance.require_allowed(job, report)
        try:
            result = operation(db, report, job)
        except RetryableTaskError as exc:
            db.delete(marker)
            job.status = ToolJobStatus.PENDING.value
            job.last_error = str(exc)
            job.run_after = datetime.utcnow()
            job.updated_at = datetime.utcnow()
            governance.finish(audit, "RETRY", str(exc))
            db.commit()
            raise
        except Exception as exc:
            job.status = ToolJobStatus.DEAD.value
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.updated_at = datetime.utcnow()
            governance.finish(audit, "FAILED", job.last_error)
            db.commit()
            raise
        job.status = ToolJobStatus.SUCCESS.value
        job.updated_at = datetime.utcnow()
        governance.finish(audit, "SUCCESS", payload=result)
        db.commit()
        return {"status": "SUCCESS", "eventId": event_id, **result}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(
    name="app.workers.tasks.process_excel",
    autoretry_for=(RetryableTaskError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def process_excel(event_id: str, report_id: int) -> dict:
    def operation(db: Session, report: PsychologicalReport, _: ToolJob) -> dict:
        record = ToolOrchestrationService(db, worker_settings).write_excel(report, commit=False)
        if record.status != ToolStatus.SUCCESS.value:
            raise RetryableTaskError(record.message)
        return {"recordId": record.id}

    return _run_governed(
        "process_excel",
        event_id,
        report_id,
        ToolJobKind.EXCEL_REPORT.value,
        operation,
    )


@celery_app.task(
    name="app.workers.tasks.create_case",
    autoretry_for=(RetryableTaskError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def create_case(event_id: str, report_id: int) -> dict:
    def operation(db: Session, report: PsychologicalReport, _: ToolJob) -> dict:
        case = ToolOrchestrationService(db, worker_settings).create_case(report, commit=False)
        if report.risk_level == "HIGH":
            OutboxService.add_event(
                db,
                "case.created",
                "case",
                case.id,
                {"reportId": report.id, "riskLevel": report.risk_level},
                f"case.created:{case.id}",
            )
        return {"caseId": case.id}

    return _run_governed(
        "create_case",
        event_id,
        report_id,
        ToolJobKind.CASE_CREATE.value,
        operation,
    )


@celery_app.task(
    name="app.workers.tasks.send_high_risk_alert",
    autoretry_for=(RetryableTaskError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_high_risk_alert(event_id: str, case_id: int) -> dict:
    db = SessionLocal()
    try:
        case = db.get(RiskCase, case_id)
        if case is None:
            raise ValueError(f"case {case_id} not found")
        report_id = case.report_id
    finally:
        db.close()

    def operation(db: Session, _: PsychologicalReport, __: ToolJob) -> dict:
        case = db.get(RiskCase, case_id)
        if case is None:
            raise ValueError(f"case {case_id} not found")
        record = ToolOrchestrationService(db, worker_settings).send_case_alert(case, commit=False)
        if record.status != ToolStatus.SUCCESS.value:
            raise RetryableTaskError(record.message)
        return {"alertId": record.id, "caseId": case_id}

    return _run_governed(
        "send_high_risk_alert",
        event_id,
        report_id,
        ToolJobKind.ALERT_SEND.value,
        operation,
    )
