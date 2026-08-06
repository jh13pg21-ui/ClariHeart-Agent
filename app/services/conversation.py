from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChatMessage, ChatSession
from app.schemas.dtos import (
    ConversationMessageResponse,
    ConversationResponse,
    ConversationSummaryResponse,
)
from app.core.config import get_settings
from app.services.data_protection import SensitiveTextProtector


class ConversationService:
    def __init__(self, db: Session):
        self.db = db
        self.protector = SensitiveTextProtector(get_settings())

    def list_for_user(self, user_id: int, limit: int = 50) -> list[ConversationSummaryResponse]:
        last_message = (
            select(ChatMessage.content)
            .where(ChatMessage.session_id == ChatSession.id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        rows = (
            self.db.query(ChatSession, last_message.label("last_message"))
            .filter(ChatSession.user_id == user_id)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
            .limit(limit)
            .all()
        )
        return [
            ConversationSummaryResponse(
                sessionId=session.public_id,
                title=session.title,
                lastMessage=self.protector.reveal(message or "")[:120],
                createdAt=session.created_at,
                updatedAt=session.updated_at,
            )
            for session, message in rows
        ]

    def get_for_user(self, user_id: int, public_id: str) -> ConversationResponse:
        session = (
            self.db.query(ChatSession)
            .filter(
                ChatSession.public_id == public_id,
                ChatSession.user_id == user_id,
            )
            .first()
        )
        if session is None:
            raise ValueError("会话不存在")
        rows = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session.id,
                ChatMessage.user_id == user_id,
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            .all()
        )
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            messages=[
                ConversationMessageResponse(
                    role=row.role,
                    content=self.protector.reveal(row.content),
                    createdAt=row.created_at,
                )
                for row in rows
            ],
        )
