from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user, require_admin
from app.models.entities import LongTermMemory, UserAccount
from app.schemas.dtos import (
    CaseActionRequest,
    ChatRequest,
    PrivacyPreferenceRequest,
    PrivacyPreferenceResponse,
    authority,
)
from app.services.chat import ChatService
from app.services.conversation import ConversationService
from app.services.knowledge import KnowledgeService
from app.services.long_term_memory import LongTermMemoryService
from app.services.model_assets import finetuned_model_status
from app.services.report import ReportService
from app.services.security_audit import SecurityAuditService
from app.services.skills import MindBridgeSkillLibrary
from app.services.tools import ToolOrchestrationService
from app.services.runtime_metrics import get_runtime_metrics

router = APIRouter()


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    service = ChatService(db, get_settings())
    return StreamingResponse(
        service.stream_chat(user, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    framework = agent_framework_status(settings)
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": framework,
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "CoordinatorAgent", "status": "READY", "description": "维护任务板、预算、安全门槛、冲突仲裁和最终采纳"},
            {"name": "UnderstandingAgent", "status": "READY", "description": "独立理解用户输入，发布 intent artifact"},
            {"name": "SafetyAgent", "status": "READY", "description": "独立风险评估、SAFETY_OVERRIDE 和候选回复安全审查"},
            {"name": "ContextAgent", "status": "READY", "description": "独立记忆视图、RAG 检索和 skill 上下文聚合"},
            {"name": "ResponseAgent", "status": "READY", "description": "根据黑板 artifact 发布候选回复方案"},
        ],
        "skills": MindBridgeSkillLibrary.status_items(),
        "runtimeHarness": {
            "name": "MindBridgeAgentHarness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "event-driven-multi-agent",
            "maxSteps": EventDrivenAgentRuntimeService.max_steps,
            "scheduler": "claim-based-actor-runtime",
        },
        "collaboration": {
            "scheduler": "claim-based",
            "state": "append-only-blackboard",
            "messageBus": "per-agent inbox over shared mailbox",
            "fixedWorkflow": False,
            "agentIsolation": {
                "prompt": "per-agent system prompt",
                "memory": "per-agent private Redis key",
                "model": "per-agent model profile",
                "tools": "per-agent tool permissions",
            },
            "taskRecovery": {
                "timeoutSeconds": settings.agent_task_timeout_seconds,
                "maxAttempts": settings.agent_task_max_attempts,
                "strategy": "failure classification + exponential backoff + task-level fallback artifact",
            },
            "memory": {
                "shortTerm": "Redis sliding window with MySQL fallback",
                "conversationSummary": "LLM structured MySQL checkpoint with Redis cache and deterministic fallback",
                "summaryRefresh": "Transactional Outbox + RabbitMQ + Celery",
                "longTerm": "user-scoped MySQL memories selected by index",
            },
        },
    }


@router.get("/api/reports/me")
def my_reports(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports(user.id)


@router.get("/api/conversations")
def my_conversations(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    return ConversationService(db).list_for_user(user.id)


@router.get("/api/conversations/{session_id}")
def my_conversation(
    session_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return ConversationService(db).get_for_user(user.id, session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/memories")
def my_long_term_memories(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    return LongTermMemoryService(
        db,
        get_settings(),
    ).responses_for_user(user.id)


@router.get("/api/privacy/preferences", response_model=PrivacyPreferenceResponse)
def my_privacy_preferences(
    user: Annotated[UserAccount, Depends(current_user)],
):
    return PrivacyPreferenceResponse(
        longTermMemoryEnabled=user.long_term_memory_enabled,
    )


@router.patch("/api/privacy/preferences", response_model=PrivacyPreferenceResponse)
def update_my_privacy_preferences(
    payload: PrivacyPreferenceRequest,
    request: Request,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    account = db.get(UserAccount, user.id)
    if account is None:
        raise HTTPException(404, "用户不存在")
    account.long_term_memory_enabled = payload.longTermMemoryEnabled
    db.add(account)
    db.flush()
    removed = 0
    if not payload.longTermMemoryEnabled and payload.purgeExistingMemories:
        removed = (
            db.query(LongTermMemory)
            .filter(LongTermMemory.user_id == user.id)
            .delete(synchronize_session=False)
        )
    SecurityAuditService(db).record(
        account,
        action="privacy_preference_update",
        resource_type="user_account",
        resource_id=str(user.id),
        outcome="success",
        ip_address=request.client.host if request.client else "",
    )
    db.commit()
    return PrivacyPreferenceResponse(
        longTermMemoryEnabled=account.long_term_memory_enabled,
        removedMemories=int(removed),
    )


@router.delete("/api/memories/{memory_id}", status_code=204)
def delete_my_long_term_memory(
    memory_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    deleted = LongTermMemoryService(
        db,
        get_settings(),
    ).delete_for_user(user.id, memory_id)
    if not deleted:
        raise HTTPException(404, "长期记忆不存在")
    return Response(status_code=204)


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).alert_records()


@router.get("/api/admin/cases")
def admin_cases(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).risk_cases()


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(case_id: int, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).case_notes(case_id)


@router.post("/api/admin/cases/{case_id}/acknowledge")
def acknowledge_admin_case(
    case_id: int,
    payload: CaseActionRequest,
    request: Request,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        case = ToolOrchestrationService(db, get_settings()).acknowledge_case(
            case_id,
            user.username,
            payload.note,
        )
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    SecurityAuditService(db).record(
        user,
        action="risk_case_acknowledge",
        resource_type="risk_case",
        resource_id=str(case_id),
        outcome="success",
        ip_address=request.client.host if request.client else "",
    )
    return {"caseId": case.id, "status": case.status, "acknowledgedBy": case.acknowledged_by}


@router.post("/api/admin/cases/{case_id}/notes")
def add_admin_case_note(
    case_id: int,
    payload: CaseActionRequest,
    request: Request,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    if not payload.note.strip():
        raise HTTPException(422, "个案备注不能为空")
    try:
        note = ToolOrchestrationService(db, get_settings()).add_case_note(
            case_id,
            user.username,
            payload.note,
        )
    except RuntimeError as exc:
        raise HTTPException(404, str(exc)) from exc
    SecurityAuditService(db).record(
        user,
        action="risk_case_note_add",
        resource_type="risk_case",
        resource_id=str(case_id),
        outcome="success",
        ip_address=request.client.host if request.client else "",
    )
    return {"id": note.id, "caseId": note.case_id, "actor": note.actor, "note": note.note}


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).dead_letters()


@router.get("/api/admin/outbox-events")
def admin_outbox_events(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).outbox_events()


@router.get("/api/admin/agent-traces")
def admin_agent_traces(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).agent_run_traces()


@router.get("/api/admin/tool-audits")
def admin_tool_audits(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_audits()


@router.get("/api/admin/runtime-metrics")
def admin_runtime_metrics(
    _: Annotated[UserAccount, Depends(require_admin)],
):
    return get_runtime_metrics().snapshot()


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(
    session_id: str,
    request: Request,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    audit = SecurityAuditService(db)
    ip_address = request.client.host if request.client else ""
    try:
        result = ReportService(db).conversation(session_id)
    except ValueError as exc:
        audit.record(
            user,
            action="admin_conversation_read",
            resource_type="chat_session",
            resource_id=session_id,
            outcome="not_found",
            ip_address=ip_address,
        )
        raise HTTPException(404, str(exc)) from exc
    except Exception:
        db.rollback()
        audit.record(
            user,
            action="admin_conversation_read",
            resource_type="chat_session",
            resource_id=session_id,
            outcome="error",
            ip_address=ip_address,
        )
        raise
    audit.record(
        user,
        action="admin_conversation_read",
        resource_type="chat_session",
        resource_id=session_id,
        outcome="success",
        ip_address=ip_address,
    )
    return result


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


