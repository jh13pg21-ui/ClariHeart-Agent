from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import uuid
from sqlalchemy.orm import Session

from app.agents.harness import MindBridgeAgentHarness
from app.agents.harness import turn_id_for
from app.core.config import Settings
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.core.enums import RiskLevel
from app.services.ai import split_text


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.agent_harness = MindBridgeAgentHarness(db, settings)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        request_id = request.requestId or uuid.uuid4().hex
        request = request.model_copy(update={"requestId": request_id})
        turn_id = turn_id_for(int(getattr(user, "id", 0) or 0), request_id)
        event_sequence = 0

        def emit(event: str, data: dict) -> str:
            nonlocal event_sequence
            event_sequence += 1
            event_id = f"{turn_id}:{event_sequence}"
            enriched = {
                **data,
                "requestId": request_id,
                "turnId": turn_id,
                "eventId": event_id,
            }
            return sse(event, enriched, event_id=event_id)

        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

        async def session_sink(session) -> None:
            await queue.put(("meta", session))

        async def response_token_sink(token: str) -> None:
            await queue.put(("token", token))

        async def run_harness() -> None:
            try:
                outcome = await self.agent_harness.run(
                    user,
                    request,
                    response_token_sink=response_token_sink,
                    session_sink=session_sink,
                )
            except Exception as exc:
                await queue.put(("error", exc))
            else:
                await queue.put(("outcome", outcome))

        runner = asyncio.create_task(run_harness())
        streamed_chunks: list[str] = []
        session_id: str | None = request.sessionId
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "meta":
                    session_id = payload.public_id
                    yield emit(
                        "meta",
                        ChatStreamEvent(
                            type="meta",
                            sessionId=payload.public_id,
                        ).model_dump(by_alias=True),
                    )
                    continue
                if kind == "token":
                    token = str(payload)
                    streamed_chunks.append(token)
                    yield emit(
                        "token",
                        ChatStreamEvent(
                            type="token",
                            sessionId=session_id,
                            content=token,
                        ).model_dump(),
                    )
                    continue
                if kind == "error":
                    reset_session = request.sessionId is None or isinstance(payload, ValueError)
                    yield emit(
                        "error",
                        ChatStreamEvent(
                            type="error",
                            message="服务暂时不可用，请稍后重试。",
                            resetSession=reset_session,
                        ).model_dump(),
                    )
                    break

                outcome = payload
                text = outcome.response_text
                risk = RiskLevel(outcome.risk_level or RiskLevel.LOW.value)
                if risk == RiskLevel.HIGH:
                    yield emit(
                        "message",
                        ChatStreamEvent(
                            type="message",
                            sessionId=outcome.session.public_id,
                            content=text,
                        ).model_dump(),
                    )
                elif streamed_chunks:
                    streamed_text = "".join(streamed_chunks).strip()
                    if streamed_text != text:
                        yield emit(
                            "replace",
                            ChatStreamEvent(
                                type="replace",
                                sessionId=outcome.session.public_id,
                                content=text,
                            ).model_dump(),
                        )
                else:
                    async for chunk in self._reviewed_chunks(text):
                        yield emit(
                            "token",
                            ChatStreamEvent(
                                type="token",
                                sessionId=outcome.session.public_id,
                                content=chunk,
                            ).model_dump(),
                        )
                if text:
                    self.agent_harness.save_assistant_message(
                        user,
                        outcome.session,
                        text,
                        extract_long_term_memory=risk != RiskLevel.HIGH,
                    )
                yield emit(
                    "done",
                    ChatStreamEvent(
                        type="done",
                        sessionId=outcome.session.public_id,
                    ).model_dump(),
                )
                break
        finally:
            if not runner.done():
                runner.cancel()
                with suppress(asyncio.CancelledError):
                    await runner

    async def _reviewed_chunks(self, text: str):
        delay_ms = int(getattr(getattr(self, "settings", None), "reviewed_stream_delay_ms", 0))
        for chunk in split_text(text, 12):
            yield chunk
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)

def sse(event: str, data: dict, *, event_id: str | None = None) -> str:
    suffix = f"id: {event_id}\n" if event_id else ""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n{suffix}\n"
