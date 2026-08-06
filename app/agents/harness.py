from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.result import AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, MessageRole, RiskLevel
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from app.services.long_term_memory import LongTermMemoryService
from app.services.memory import RedisShortTermMemoryStore
from app.services.outbox import OutboxService
from app.services.privacy import PrivacySanitizer
from app.services.trace import AgentTraceService
from app.services.data_protection import SensitiveTextProtector
from app.services.output_safety import OutputSafetyDecision


@dataclass
class AgentToolPlan:
    report_id: int | None
    risk_level: str | None

    @property
    def requires_tools(self) -> bool:
        return self.report_id is not None


@dataclass
class AgentHarnessOutcome:
    session: ChatSession
    original_input: str
    model_input: str
    intent: IntentType
    risk_level: str | None
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    response_text: str
    output_safety: OutputSafetyDecision
    agent_steps: list[AgentStep]
    retrieved_knowledge: list[SearchResult]
    report_id: int | None
    tool_plan: AgentToolPlan
    trace_id: int | None


class MindBridgeAgentHarness:
    """Runtime harness for one MindBridge agent turn.

    The harness owns business orchestration around the agent runtime. HTTP/SSE
    code can stay thin while this class manages input preparation, persistence,
    risk report creation, tool planning, and trace data.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.protector = SensitiveTextProtector(settings)
        self.memory = RedisShortTermMemoryStore(settings)

    async def run(
        self,
        user: UserAccount,
        request: ChatRequest,
        response_token_sink: Callable[[str], Awaitable[None]] | None = None,
        session_sink: Callable[[ChatSession], Awaitable[None]] | None = None,
    ) -> AgentHarnessOutcome:
        original_input = request.message.strip()
        model_input = self.privacy.sanitize(original_input)
        session = self._resolve_session(user, request.sessionId, original_input)
        if session_sink is not None:
            await session_sink(session)
        agent_run = await create_agent_runtime(self.db, self.settings).run(
            user,
            session,
            original_input,
            model_input,
            response_token_sink=response_token_sink,
        )
        risk_level = agent_run.risk_level.value
        try:
            self._add_message(user, session, MessageRole.USER, original_input)
            report = self._create_report(user, session, original_input, agent_run)
            trace = AgentTraceService(self.db, self.settings).save_run(
                user=user,
                session=session,
                original_input=original_input,
                sanitized_input=model_input,
                memory_brief=agent_run.memory_brief,
                agent_run=agent_run,
                report_id=report.id if report is not None else None,
                commit=False,
            )
            if report is not None:
                self._add_report_events(report)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.memory.append(session.public_id, MessageRole.USER.value, original_input)
        tool_plan = AgentToolPlan(report_id=report.id if report is not None else None, risk_level=risk_level)
        return AgentHarnessOutcome(
            session=session,
            original_input=original_input,
            model_input=model_input,
            intent=agent_run.intent,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            response_text=agent_run.response_text,
            output_safety=agent_run.output_safety,
            agent_steps=agent_run.steps,
            retrieved_knowledge=agent_run.retrieved_knowledge,
            report_id=report.id if report is not None else None,
            tool_plan=tool_plan,
            trace_id=trace.id,
        )

    def save_assistant_message(
        self,
        user: UserAccount,
        session: ChatSession,
        content: str,
        extract_long_term_memory: bool = True,
    ) -> None:
        message = self._add_message(
            user,
            session,
            MessageRole.ASSISTANT,
            content,
        )
        self.db.flush()
        if extract_long_term_memory and LongTermMemoryService(
            self.db,
            self.settings,
        ).should_schedule_extraction(session, message):
            OutboxService.add_event(
                self.db,
                "memory.extract",
                "chat_message",
                message.id,
                {"riskLevel": RiskLevel.LOW.value},
                f"memory.extract:{message.id}",
            )
        self.db.commit()
        self.memory.append(
            session.public_id,
            MessageRole.ASSISTANT.value,
            content,
        )

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> None:
        self._add_message(user, session, role, content)
        self.db.commit()
        self.memory.append(session.public_id, role.value, content)

    def _add_message(
        self,
        user: UserAccount,
        session: ChatSession,
        role: MessageRole,
        content: str,
    ) -> ChatMessage:
        privacy = getattr(self, "privacy", None) or PrivacySanitizer()
        protector = getattr(self, "protector", None) or SensitiveTextProtector(self.settings)
        safe_content = privacy.sanitize(content)
        stored_content = (
            content
            if getattr(self.settings, "privacy_store_original_input", False)
            else safe_content
        )
        message = ChatMessage(
            user_id=user.id,
            session_id=session.id,
            role=role.value,
            content=protector.protect(stored_content),
        )
        self.db.add(message)
        session.touch()
        self.db.add(session)
        return message

    def _resolve_session(self, user: UserAccount, public_id: str | None, text: str) -> ChatSession:
        if public_id:
            session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id).first()
            if session is None:
                raise ValueError("Session not found")
            return session
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=self.privacy.sanitize(text)[:36])
        self.db.add(session)
        self.db.flush()
        return session

    def _create_report(self, user: UserAccount, session: ChatSession, text: str, agent_run) -> PsychologicalReport | None:
        if not agent_run.requires_report or agent_run.assessment is None:
            return None
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=self.protector.protect(
                text
                if getattr(self.settings, "privacy_store_original_input", False)
                else self.privacy.sanitize(text)
            ),
            intent=agent_run.intent.value,
            emotion=agent_run.assessment.emotion.value,
            emotion_score=agent_run.assessment.emotion_score,
            risk_level=agent_run.assessment.risk.value,
            confidence=agent_run.assessment.confidence,
            summary=agent_run.assessment.summary,
        )
        self.db.add(report)
        self.db.flush()
        return report

    def _add_report_events(self, report: PsychologicalReport) -> None:
        payload = {"reportId": report.id, "riskLevel": report.risk_level}
        OutboxService.add_event(
            self.db,
            "report.excel",
            "report",
            report.id,
            payload,
            f"report.excel:{report.id}",
        )
        if report.risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
            OutboxService.add_event(
                self.db,
                "case.create",
                "report",
                report.id,
                payload,
                f"case.create:{report.id}",
            )
