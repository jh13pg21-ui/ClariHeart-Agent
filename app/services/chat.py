from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.core.enums import RiskLevel
from app.services.ai import split_text


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.agent_harness = MindBridgeAgentHarness(db, settings)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        outcome = await self.agent_harness.run(user, request)
        yield sse("meta", ChatStreamEvent(type="meta", sessionId=outcome.session.public_id).model_dump(by_alias=True))
        text = outcome.response_text
        risk = RiskLevel(outcome.risk_level or RiskLevel.LOW.value)
        if risk == RiskLevel.HIGH:
            yield sse(
                "message",
                ChatStreamEvent(
                    type="message",
                    sessionId=outcome.session.public_id,
                    content=text,
                ).model_dump(),
            )
        else:
            for token in split_text(text, 12):
                yield sse(
                    "token",
                    ChatStreamEvent(
                        type="token",
                        sessionId=outcome.session.public_id,
                        content=token,
                    ).model_dump(),
                )
        if text:
            self.agent_harness.save_assistant_message(user, outcome.session, text)
        try:
            await self.agent_harness.dispatch_tools(outcome.tool_plan)
        except Exception as exc:
            logger.warning(
                "Post-response tool dispatch failed for session=%s report_id=%s: %s",
                outcome.session.public_id,
                outcome.report_id,
                exc,
                exc_info=True,
            )
        yield sse("done", ChatStreamEvent(type="done", sessionId=outcome.session.public_id).model_dump())


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
