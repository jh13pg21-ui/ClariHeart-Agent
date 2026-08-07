from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.enums import ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import (
    ProcessedMessage,
    ChatMessage,
    ChatSession,
    AlertRecord,
    DeadLetterRecord,
    PsychologicalReport,
    RiskCase,
    MemoryConsolidationRun,
    ToolJob,
)
from app.services.outbox import OutboxService
from app.services.conversation_summary import ConversationSummaryService
from app.services.long_term_memory import LongTermMemoryService
from app.services.memory_consolidation import MemoryConsolidationService
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy_retention import PrivacyRetentionService
from app.services.tool_governance import ToolGovernanceService
from app.services.tools import ToolOrchestrationService
from app.services.runtime_metrics import get_runtime_metrics
from app.workers.celery_app import celery_app


worker_settings = get_settings()


class RetryableTaskError(RuntimeError):
    pass


@celery_app.task(
    name="app.workers.tasks.purge_expired_private_data",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_private_data() -> dict:
    db: Session = SessionLocal()
    try:
        result = PrivacyRetentionService(db, worker_settings).purge_expired()
        return {
            "status": "SUCCESS",
            "messages": result.messages,
            "sessions": result.sessions,
            "reports": result.reports,
            "traces": result.traces,
            "summaries": result.summaries,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(
    name="app.workers.tasks.extract_long_term_memory",
    autoretry_for=(RetryableTaskError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def extract_long_term_memory(
    event_id: str,
    assistant_message_id: int,
) -> dict:
    db: Session = SessionLocal()
    try:
        marker = _claim_message(
            db,
            "extract_long_term_memory",
            event_id,
        )
        if marker is None:
            return {"status": "ALREADY_PROCESSED", "eventId": event_id}
        message = db.get(ChatMessage, assistant_message_id)
        if message is None:
            db.commit()
            return {
                "status": "NOT_FOUND",
                "eventId": event_id,
                "messageId": assistant_message_id,
            }
        try:
            stored = asyncio.run(
                LongTermMemoryService(
                    db,
                    worker_settings,
                ).extract_from_assistant_message(assistant_message_id)
            )
        except Exception as exc:
            db.delete(marker)
            db.commit()
            raise RetryableTaskError(
                f"长期记忆提取失败：{type(exc).__name__}: {exc}"
            ) from exc
        session = db.get(ChatSession, message.session_id)
        if session is not None:
            session.long_term_memory_extracted_message_id = message.id
            db.add(session)
        consolidation_run = MemoryConsolidationService(
            db,
            worker_settings,
        ).reserve_schedule(message.user_id)
        if consolidation_run is not None:
            OutboxService.add_event(
                db,
                "memory.consolidate",
                "user",
                message.user_id,
                {
                    "riskLevel": None,
                    "consolidationRunId": consolidation_run.public_id,
                },
                f"memory.consolidate:{consolidation_run.public_id}",
            )
        db.commit()
        get_runtime_metrics().record_memory("extraction", "success", len(stored))
        return {
            "status": "SUCCESS",
            "eventId": event_id,
            "messageId": assistant_message_id,
            "stored": len(stored),
        }
    except RetryableTaskError:
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(
    name="app.workers.tasks.consolidate_long_term_memory",
    autoretry_for=(RetryableTaskError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def consolidate_long_term_memory(
    event_id: str,
    user_id: int,
    run_public_id: str,
) -> dict:
    db: Session = SessionLocal()
    try:
        marker = _claim_message(
            db,
            "consolidate_long_term_memory",
            event_id,
        )
        if marker is None:
            return {"status": "ALREADY_PROCESSED", "eventId": event_id}
        run = (
            db.query(MemoryConsolidationRun)
            .filter(
                MemoryConsolidationRun.public_id == run_public_id,
                MemoryConsolidationRun.user_id == user_id,
            )
            .first()
        )
        if run is None:
            db.commit()
            return {
                "status": "NOT_FOUND",
                "eventId": event_id,
                "runId": run_public_id,
            }
        result = asyncio.run(
            MemoryConsolidationService(
                db,
                worker_settings,
            ).consolidate(user_id, run=run)
        )
        if result.status == "FAILED":
            db.rollback()
            raise RetryableTaskError(result.error or "记忆整合暂时失败")
        db.commit()
        get_runtime_metrics().record_memory(
            "consolidation",
            result.status.lower(),
            1,
        )
        return {
            "status": result.status,
            "eventId": event_id,
            "runId": run_public_id,
            "kept": result.kept,
            "merged": result.merged,
            "superseded": result.superseded,
            "expired": result.expired,
        }
    except RetryableTaskError:
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(
    name="app.workers.tasks.refresh_conversation_summary",
    autoretry_for=(RetryableTaskError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def refresh_conversation_summary(
    event_id: str,
    assistant_message_id: int,
) -> dict:
    db: Session = SessionLocal()
    try:
        marker = _claim_message(
            db,
            "refresh_conversation_summary",
            event_id,
        )
        if marker is None:
            return {"status": "ALREADY_PROCESSED", "eventId": event_id}
        message = db.get(ChatMessage, assistant_message_id)
        if message is None:
            db.commit()
            return {
                "status": "NOT_FOUND",
                "eventId": event_id,
                "messageId": assistant_message_id,
            }
        memory = RedisShortTermMemoryStore(worker_settings)
        service = ConversationSummaryService(
            db,
            worker_settings,
            memory=memory,
        )
        try:
            record = asyncio.run(
                service.ensure_through(
                    message.session,
                    assistant_message_id,
                    reason="async_outbox",
                )
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            raise RetryableTaskError(
                f"结构化会话摘要刷新失败：{type(exc).__name__}: {exc}"
            ) from exc
        if record is None:
            return {
                "status": "NOT_DUE",
                "eventId": event_id,
                "messageId": assistant_message_id,
            }
        service.cache_record(record)
        return {
            "status": "SUCCESS",
            "eventId": event_id,
            "messageId": assistant_message_id,
            "throughMessageId": record.through_message_id,
            "summaryStatus": record.status,
        }
    except RetryableTaskError:
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
    job = (
        db.query(ToolJob)
        .filter(
            ToolJob.report_id == report_id,
            ToolJob.kind == kind,
            ToolJob.status == ToolJobStatus.PENDING.value,
        )
        .order_by(ToolJob.id.desc())
        .first()
    )
    if job is None:
        job = ToolJob(
            report_id=report_id,
            kind=kind,
            attempts=0,
            max_attempts=max(1, int(getattr(worker_settings, "tool_task_max_attempts", 5))),
            run_after=datetime.utcnow(),
            last_error="",
        )
    job.status = ToolJobStatus.RUNNING.value
    job.attempts += 1
    job.updated_at = datetime.utcnow()
    db.add(job)
    db.flush()
    return job


def _dead_letter(db: Session, job: ToolJob, reason: str, payload: dict | None = None) -> DeadLetterRecord:
    record = DeadLetterRecord(
        job_id=job.id,
        report_id=job.report_id,
        kind=job.kind,
        reason=reason[:2000],
        payload=json.dumps(payload or {}, ensure_ascii=False, default=str),
    )
    db.add(record)
    return record


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
            error = str(exc)
            job.last_error = error
            job.updated_at = datetime.utcnow()
            if job.attempts >= job.max_attempts:
                job.status = ToolJobStatus.DEAD.value
                _dead_letter(
                    db,
                    job,
                    error,
                    {"eventId": event_id, "attempts": job.attempts},
                )
                governance.finish(audit, "DEAD", error)
                db.commit()
                return {
                    "status": "DEAD",
                    "eventId": event_id,
                    "jobId": job.id,
                    "reason": error,
                }
            db.delete(marker)
            job.status = ToolJobStatus.PENDING.value
            job.run_after = datetime.utcnow()
            governance.finish(audit, "RETRY", error)
            db.commit()
            raise
        except Exception as exc:
            job.status = ToolJobStatus.DEAD.value
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.updated_at = datetime.utcnow()
            _dead_letter(
                db,
                job,
                job.last_error,
                {"eventId": event_id, "attempts": job.attempts},
            )
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
        limit = max(1, int(getattr(worker_settings, "alert_email_rate_limit_per_minute", 30)))
        delivered = (
            db.query(AlertRecord)
            .filter(
                AlertRecord.status == ToolStatus.SUCCESS.value,
                AlertRecord.created_at >= datetime.utcnow() - timedelta(minutes=1),
            )
            .count()
        )
        if delivered >= limit:
            raise RetryableTaskError(f"高风险预警达到每分钟限流阈值 {limit}")
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
