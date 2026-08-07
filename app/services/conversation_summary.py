from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models.entities import ChatMessage, ChatSession, ConversationMemorySummary
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from app.services.data_protection import SensitiveTextProtector
from app.services.memory import compact_history_for_prompt, summarize_history_for_memory
from app.services.privacy import PrivacySanitizer
from app.prompts.runtime import registered_complete, registered_task_messages


SUMMARY_SCHEMA_VERSION = 2
SUMMARY_PROMPT_VERSION = "2026.08-v1:conversation_summary@2.0.0"
SUMMARY_STATUS_LLM = "LLM"
SUMMARY_STATUS_FALLBACK = "FALLBACK"

_SAFETY_TERMS = (
    "不想活",
    "结束生命",
    "自杀",
    "自残",
    "伤害自己",
    "伤害别人",
    "轻生",
    "suicide",
    "kill myself",
    "self harm",
)
_DIAGNOSIS_TERMS = (
    "确诊",
    "诊断为",
    "抑郁症",
    "焦虑症",
    "双相",
    "精神分裂",
    "风险等级",
)


@dataclass(frozen=True)
class SummaryItem:
    summary: str
    evidence_message_ids: tuple[int, ...]
    topic: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": self.summary,
            "evidenceMessageIds": list(self.evidence_message_ids),
        }
        if self.topic:
            payload["topic"] = self.topic
        return payload


@dataclass(frozen=True)
class SafetyContinuity:
    follow_up_needed: bool = False
    summary: str = ""
    evidence_message_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "followUpNeeded": self.follow_up_needed,
            "summary": self.summary,
            "evidenceMessageIds": list(self.evidence_message_ids),
        }


@dataclass(frozen=True)
class StructuredConversationSummary:
    schema_version: int = SUMMARY_SCHEMA_VERSION
    student_concerns: tuple[SummaryItem, ...] = ()
    preferences: tuple[SummaryItem, ...] = ()
    effective_supports: tuple[SummaryItem, ...] = ()
    unresolved_threads: tuple[SummaryItem, ...] = ()
    safety_continuity: SafetyContinuity = field(default_factory=SafetyContinuity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "studentConcerns": [item.to_dict() for item in self.student_concerns],
            "preferences": [item.to_dict() for item in self.preferences],
            "effectiveSupports": [item.to_dict() for item in self.effective_supports],
            "unresolvedThreads": [item.to_dict() for item in self.unresolved_threads],
            "safetyContinuity": self.safety_continuity.to_dict(),
        }

    def evidence_ids(self) -> set[int]:
        result: set[int] = set(self.safety_continuity.evidence_message_ids)
        for items in (
            self.student_concerns,
            self.preferences,
            self.effective_supports,
            self.unresolved_threads,
        ):
            for item in items:
                result.update(item.evidence_message_ids)
        return result

    def format_for_prompt(self, max_chars: int) -> str:
        lines: list[str] = []
        if self.safety_continuity.follow_up_needed:
            lines.append("安全连续性：存在需要后续关注的安全信号；本轮仍须独立复核。")
        sections = (
            ("待继续事项", self.unresolved_threads),
            ("学生持续关注", self.student_concerns),
            ("互动偏好", self.preferences),
            ("已确认有效的支持", self.effective_supports),
        )
        for label, items in sections:
            if items:
                lines.append(f"{label}：" + "；".join(item.summary for item in items))
        if not lines:
            return "无相关历史记忆。"
        return _bounded_lines(lines, max_chars)


class ConversationSummaryService:
    """维护按会话隔离的结构化摘要检查点，并为 Agent 生成有界上下文。"""

    def __init__(
        self,
        db: Session,
        settings,
        memory=None,
        ai: AiClient | None = None,
    ):
        self.db = db
        self.settings = settings
        self.memory = memory
        self.ai = ai
        self.privacy = PrivacySanitizer()
        self.protector = SensitiveTextProtector(settings)

    def should_schedule_refresh(
        self,
        session: ChatSession,
        assistant_message: ChatMessage,
    ) -> bool:
        if not getattr(self.settings, "memory_compaction_enabled", True):
            return False
        recent_count = max(
            2,
            int(getattr(self.settings, "memory_compaction_recent_messages", 8)),
        )
        refresh_count = max(
            1,
            int(getattr(self.settings, "memory_summary_refresh_messages", 4)),
        )
        try:
            record = (
                self.db.query(ConversationMemorySummary)
                .filter(ConversationMemorySummary.session_id == session.id)
                .first()
            )
            watermark = record.through_message_id if record else 0
            pending = (
                self.db.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session.id,
                    ChatMessage.id > (watermark or 0),
                    ChatMessage.id <= assistant_message.id,
                )
                .count()
            )
            return pending >= recent_count + refresh_count
        except Exception:
            return False

    async def refresh_for_assistant_message(
        self,
        assistant_message_id: int,
    ) -> ConversationMemorySummary | None:
        assistant = self.db.get(ChatMessage, assistant_message_id)
        if assistant is None or assistant.role.upper() != "ASSISTANT":
            return None
        session = self.db.get(ChatSession, assistant.session_id)
        if session is None:
            return None

        record = (
            self.db.query(ConversationMemorySummary)
            .filter(ConversationMemorySummary.session_id == session.id)
            .with_for_update()
            .first()
        )
        watermark = record.through_message_id if record else 0
        max_source_messages = max(
            12,
            int(getattr(self.settings, "memory_summary_max_source_messages", 40)),
        )
        rows = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session.id,
                ChatMessage.id > (watermark or 0),
                ChatMessage.id <= assistant.id,
            )
            .order_by(ChatMessage.id.desc())
            .limit(max_source_messages)
            .all()
        )
        rows = list(reversed(rows))
        recent_count = max(
            2,
            int(getattr(self.settings, "memory_compaction_recent_messages", 8)),
        )
        refresh_count = max(
            1,
            int(getattr(self.settings, "memory_summary_refresh_messages", 4)),
        )
        compactable = len(rows) - recent_count
        compact_count = (compactable // refresh_count) * refresh_count
        if compact_count < refresh_count:
            return None
        source_rows = rows[:compact_count]
        previous = self._summary_from_record(record) if record else StructuredConversationSummary()
        messages = [self._source_message(row) for row in source_rows]
        summary, status, error = await self._generate_or_fallback(previous, messages)
        provider, model = self._model_identity()

        if record is None:
            record = ConversationMemorySummary(
                session_id=session.id,
                schema_version=SUMMARY_SCHEMA_VERSION,
                summary_json="{}",
            )
        record.schema_version = SUMMARY_SCHEMA_VERSION
        record.summary_json = self.protector.protect(
            json.dumps(summary.to_dict(), ensure_ascii=False, separators=(",", ":"))
        )
        record.through_message_id = source_rows[-1].id
        record.source_message_count = int(record.source_message_count or 0) + len(source_rows)
        record.model_provider = provider
        record.model_name = model
        record.prompt_version = SUMMARY_PROMPT_VERSION
        record.status = status
        record.last_error = self.privacy.sanitize(error)[:1000]
        record.updated_at = datetime.utcnow()
        self.db.add(record)
        self.db.flush()
        return record

    def load_prompt_history(
        self,
        session: ChatSession,
        fallback_history: list[AiMessage],
    ) -> tuple[list[AiMessage], str]:
        snapshot = self._load_snapshot(session)
        if snapshot is None:
            if self.memory is not None and hasattr(self.memory, "prompt_history"):
                return self.memory.prompt_history(session.public_id, fallback_history)
            return compact_history_for_prompt(fallback_history, self.settings)

        summary, through_message_id = snapshot
        pending = self._messages_after(session, through_message_id)
        if pending is None:
            pending = fallback_history
        recent_count = max(
            2,
            int(getattr(self.settings, "memory_compaction_recent_messages", 8)),
        )
        brief = summary.format_for_prompt(
            max(120, int(getattr(self.settings, "memory_summary_max_chars", 500)))
        )
        recent = pending
        if len(pending) > recent_count:
            overflow = pending[:-recent_count]
            transient = summarize_history_for_memory(
                overflow,
                max_chars=max(120, int(getattr(self.settings, "memory_summary_max_chars", 500)) // 2),
            )
            if transient and transient != "无相关历史记忆。":
                brief = _bounded_lines(
                    [brief, "等待异步压缩的上下文：" + transient],
                    max(120, int(getattr(self.settings, "memory_summary_max_chars", 500))),
                )
            recent = pending[-recent_count:]
        prompt = list(recent)
        if brief and brief != "无相关历史记忆。":
            prompt.insert(
                0,
                AiMessage(
                    role="system",
                    content=(
                        "结构化历史摘要（仅供 MindBridge 内部上下文使用；不得向学生复述后台字段；"
                        "不得据此直接下诊断或跳过本轮安全复核）：\n" + brief
                    ),
                ),
            )
        return prompt, brief or "无相关历史记忆。"

    def cache_record(self, record: ConversationMemorySummary) -> None:
        if self.memory is None or not hasattr(self.memory, "save_structured_summary_cache"):
            return
        try:
            summary = self._summary_from_record(record)
            self.memory.save_structured_summary_cache(
                str(record.session.public_id),
                json.dumps(
                    {
                        "schemaVersion": SUMMARY_SCHEMA_VERSION,
                        "throughMessageId": record.through_message_id,
                        "summary": summary.to_dict(),
                        "status": record.status,
                        "updatedAt": record.updated_at.isoformat(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        except Exception:
            return

    async def _generate_or_fallback(
        self,
        previous: StructuredConversationSummary,
        messages: list[dict[str, Any]],
    ) -> tuple[StructuredConversationSummary, str, str]:
        if not getattr(self.settings, "memory_summary_llm_enabled", True):
            return self._fallback_summary(previous, messages), SUMMARY_STATUS_FALLBACK, "LLM摘要已关闭"
        allowed_ids = previous.evidence_ids() | {int(item["id"]) for item in messages}
        attempts = max(1, int(getattr(self.settings, "memory_summary_llm_attempts", 2)))
        last_error = ""
        for attempt in range(attempts):
            try:
                raw = await registered_complete(
                    self._ai_client(),
                    agent_name="ConversationSummaryService",
                    agent_prompt_id="agent.context",
                    task_name="conversation_summary",
                    payload=self._summary_payload(previous, messages, attempt, last_error),
                )
                payload = json.loads(_strip_code_fence(raw))
                summary = self._parse_summary(payload, allowed_ids)
                if _contains_safety_signal(" ".join(str(item["content"]) for item in messages)):
                    evidence = tuple(
                        int(item["id"])
                        for item in messages
                        if _contains_safety_signal(str(item["content"]))
                    )
                    summary = StructuredConversationSummary(
                        schema_version=summary.schema_version,
                        student_concerns=summary.student_concerns,
                        preferences=summary.preferences,
                        effective_supports=summary.effective_supports,
                        unresolved_threads=summary.unresolved_threads,
                        safety_continuity=SafetyContinuity(
                            True,
                            "存在需要后续关注的安全信号；后续轮次仍需独立复核。",
                            evidence,
                        ),
                    )
                if _requires_non_empty_summary(messages) and not _summary_has_content(summary):
                    raise ValueError("存在实质性用户表达，但模型返回了空摘要")
                if _contains_preference_signal(messages) and not summary.preferences:
                    raise ValueError("对话包含明确互动偏好，但模型未写入preferences")
                return summary, SUMMARY_STATUS_LLM, ""
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        return self._fallback_summary(previous, messages), SUMMARY_STATUS_FALLBACK, last_error

    def _summary_prompt(
        self,
        previous: StructuredConversationSummary,
        messages: list[dict[str, Any]],
        attempt: int,
        previous_error: str,
    ) -> list[AiMessage]:
        max_input_chars = max(
            2000,
            int(getattr(self.settings, "memory_summary_input_max_chars", 12000)),
        )
        bounded_messages: list[dict[str, Any]] = []
        consumed = 0
        for item in messages:
            content = str(item["content"])[:600]
            if consumed + len(content) > max_input_chars:
                content = content[: max(0, max_input_chars - consumed)]
            if not content:
                break
            bounded_messages.append({"id": item["id"], "role": item["role"], "content": content})
            consumed += len(content)
        retry_rule = (
            f"上次输出未通过校验（{previous_error[:180]}）；本次必须修正字段结构并返回合法 JSON。"
            if attempt
            else ""
        )
        return registered_task_messages(
            "agent.context",
            "task.conversation_summary",
            {
                "schemaVersion": SUMMARY_SCHEMA_VERSION,
                "existingSummary": previous.to_dict(),
                "newMessages": bounded_messages,
                "retryCorrection": retry_rule,
            },
        )

    def _summary_payload(
        self,
        previous: StructuredConversationSummary,
        messages: list[dict[str, Any]],
        attempt: int,
        previous_error: str,
    ) -> dict[str, Any]:
        prompt = self._summary_prompt(previous, messages, attempt, previous_error)
        return json.loads(prompt[-1].content)["payload"]

    def _parse_summary(
        self,
        payload: Any,
        allowed_ids: set[int],
    ) -> StructuredConversationSummary:
        if not isinstance(payload, dict):
            raise ValueError("摘要结果不是JSON对象")
        safety_payload = payload.get("safetyContinuity") or {}
        if not isinstance(safety_payload, dict):
            raise ValueError("safetyContinuity格式错误")
        follow_up = bool(safety_payload.get("followUpNeeded", False))
        safety_ids = _valid_evidence_ids(
            safety_payload.get("evidenceMessageIds", []),
            allowed_ids,
        )
        safety_summary = (
            "存在需要后续关注的安全信号；后续轮次仍需独立复核。"
            if follow_up
            else ""
        )
        return StructuredConversationSummary(
            student_concerns=self._parse_items(payload.get("studentConcerns"), allowed_ids),
            preferences=self._parse_items(payload.get("preferences"), allowed_ids),
            effective_supports=self._parse_items(payload.get("effectiveSupports"), allowed_ids),
            unresolved_threads=self._parse_items(payload.get("unresolvedThreads"), allowed_ids),
            safety_continuity=SafetyContinuity(follow_up, safety_summary, safety_ids),
        )

    def _parse_items(
        self,
        raw_items: Any,
        allowed_ids: set[int],
    ) -> tuple[SummaryItem, ...]:
        if raw_items is None:
            return ()
        if not isinstance(raw_items, list):
            raise ValueError("摘要条目不是数组")
        result: list[SummaryItem] = []
        for raw in raw_items[:6]:
            if not isinstance(raw, dict):
                raise ValueError("摘要数组元素必须是JSON对象")
            summary = _clean_text(self.privacy.sanitize(str(raw.get("summary", ""))), 180)
            topic = _clean_text(self.privacy.sanitize(str(raw.get("topic", ""))), 64)
            evidence = _valid_evidence_ids(raw.get("evidenceMessageIds", []), allowed_ids)
            if summary and not evidence:
                raise ValueError("摘要条目缺少合法证据消息ID")
            lowered = f"{topic} {summary}".lower()
            if (
                not summary
                or any(term.lower() in lowered for term in (*_SAFETY_TERMS, *_DIAGNOSIS_TERMS))
            ):
                continue
            result.append(SummaryItem(summary, evidence, topic))
        return tuple(_dedupe_items(result))

    def _fallback_summary(
        self,
        previous: StructuredConversationSummary,
        messages: list[dict[str, Any]],
    ) -> StructuredConversationSummary:
        concerns = list(previous.student_concerns)
        preferences = list(previous.preferences)
        unresolved = list(previous.unresolved_threads)
        safety_ids = list(previous.safety_continuity.evidence_message_ids)
        follow_up = previous.safety_continuity.follow_up_needed
        for item in messages:
            if str(item["role"]).upper() != "USER":
                continue
            message_id = int(item["id"])
            content = _clean_text(self.privacy.sanitize(str(item["content"])), 180)
            if not content:
                continue
            if _contains_safety_signal(content):
                follow_up = True
                safety_ids.append(message_id)
                continue
            if any(term.lower() in content.lower() for term in _DIAGNOSIS_TERMS):
                continue
            preference = re.search(r"(?:我|以后)(?:更)?(?:喜欢|希望|偏好)[，,:：\s]*(.+)", content)
            if preference:
                value = _clean_text(preference.group(1), 120)
                if value:
                    preferences.append(SummaryItem(value, (message_id,), "互动偏好"))
            concerns.append(SummaryItem(content, (message_id,), "近期关注"))
            unresolved = [SummaryItem(content, (message_id,), "待继续确认")]
        return StructuredConversationSummary(
            student_concerns=tuple(_dedupe_items(concerns)[-6:]),
            preferences=tuple(_dedupe_items(preferences)[-6:]),
            effective_supports=previous.effective_supports[-6:],
            unresolved_threads=tuple(_dedupe_items(unresolved)[-6:]),
            safety_continuity=SafetyContinuity(
                follow_up,
                "存在需要后续关注的安全信号；后续轮次仍需独立复核。" if follow_up else "",
                tuple(dict.fromkeys(safety_ids)),
            ),
        )

    def _load_snapshot(
        self,
        session: ChatSession,
    ) -> tuple[StructuredConversationSummary, int] | None:
        try:
            record = (
                self.db.query(ConversationMemorySummary)
                .filter(ConversationMemorySummary.session_id == session.id)
                .first()
            )
            if record is not None:
                return self._summary_from_record(record), int(record.through_message_id or 0)
        except Exception:
            pass
        if self.memory is None or not hasattr(self.memory, "load_structured_summary_cache"):
            return None
        try:
            raw = self.memory.load_structured_summary_cache(session.public_id)
            payload = json.loads(raw) if raw else None
            if not isinstance(payload, dict):
                return None
            return (
                self._parse_summary(payload.get("summary"), _all_evidence_ids(payload.get("summary"))),
                int(payload.get("throughMessageId") or 0),
            )
        except Exception:
            return None

    def _messages_after(
        self,
        session: ChatSession,
        through_message_id: int,
    ) -> list[AiMessage] | None:
        try:
            limit = max(2, int(getattr(self.settings, "redis_memory_max_messages", 40)))
            rows = (
                self.db.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session.id,
                    ChatMessage.id > through_message_id,
                )
                .order_by(ChatMessage.id.desc())
                .limit(limit)
                .all()
            )
            rows.reverse()
            return [
                AiMessage(
                    role=row.role.lower(),
                    content=self.privacy.sanitize(self.protector.reveal(row.content)),
                )
                for row in rows
            ]
        except Exception:
            return None

    def _summary_from_record(
        self,
        record: ConversationMemorySummary | None,
    ) -> StructuredConversationSummary:
        if record is None:
            return StructuredConversationSummary()
        try:
            payload = json.loads(self.protector.reveal(record.summary_json))
            return self._parse_summary(payload, _all_evidence_ids(payload))
        except Exception:
            return StructuredConversationSummary()

    def _source_message(self, row: ChatMessage) -> dict[str, Any]:
        return {
            "id": int(row.id),
            "role": row.role.upper(),
            "content": self.privacy.sanitize(self.protector.reveal(row.content)),
        }

    def _ai_client(self) -> AiClient:
        if self.ai is None:
            self.ai = AiClient(self.settings)
        return self.ai

    def _model_identity(self) -> tuple[str, str]:
        provider = str(getattr(self.settings, "ai_provider", "mock")).lower()
        if provider == "ollama":
            return provider, str(getattr(self.settings, "ollama_model", ""))
        if provider == "openai":
            return provider, str(getattr(self.settings, "openai_model", ""))
        return provider, "mock"


def _strip_code_fence(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _valid_evidence_ids(raw: Any, allowed_ids: set[int]) -> tuple[int, ...]:
    if not isinstance(raw, list):
        return ()
    result: list[int] = []
    for value in raw:
        try:
            message_id = int(value)
        except (TypeError, ValueError):
            continue
        if message_id > 0 and message_id in allowed_ids and message_id not in result:
            result.append(message_id)
    return tuple(result)


def _all_evidence_ids(payload: Any) -> set[int]:
    if not isinstance(payload, dict):
        return set()
    result: set[int] = set()
    for key in (
        "studentConcerns",
        "preferences",
        "effectiveSupports",
        "unresolvedThreads",
    ):
        for item in payload.get(key, []) if isinstance(payload.get(key, []), list) else []:
            if isinstance(item, dict):
                for value in item.get("evidenceMessageIds", []):
                    try:
                        result.add(int(value))
                    except (TypeError, ValueError):
                        continue
    safety = payload.get("safetyContinuity")
    if isinstance(safety, dict):
        for value in safety.get("evidenceMessageIds", []):
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
    return {value for value in result if value > 0}


def _dedupe_items(items: Iterable[SummaryItem]) -> list[SummaryItem]:
    result: list[SummaryItem] = []
    positions: dict[str, int] = {}
    for item in items:
        key = re.sub(r"\s+", "", item.summary.lower())
        if not key:
            continue
        if key in positions:
            result[positions[key]] = item
        else:
            positions[key] = len(result)
            result.append(item)
    return result


def _clean_text(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    return normalized[:limit]


def _contains_safety_signal(text: str) -> bool:
    lowered = str(text or "").lower().replace(" ", "")
    return any(term.lower().replace(" ", "") in lowered for term in _SAFETY_TERMS)


def _summary_has_content(summary: StructuredConversationSummary) -> bool:
    return bool(
        summary.student_concerns
        or summary.preferences
        or summary.effective_supports
        or summary.unresolved_threads
        or summary.safety_continuity.follow_up_needed
    )


def _requires_non_empty_summary(messages: list[dict[str, Any]]) -> bool:
    trivial = {
        "你好",
        "您好",
        "谢谢",
        "感谢",
        "好的",
        "好",
        "嗯",
        "是",
        "不是",
        "再见",
    }
    for item in messages:
        if str(item.get("role", "")).upper() != "USER":
            continue
        content = re.sub(r"[\s，。！？,.!?]", "", str(item.get("content", "")))
        if content and content not in trivial and len(content) >= 4:
            return True
    return False


def _contains_preference_signal(messages: list[dict[str, Any]]) -> bool:
    pattern = re.compile(
        r"(?:我|以后)(?:更)?(?:喜欢|希望|偏好)|(?:请|希望)(?:优先|先)|先给(?:我)?(?:结论|重点|步骤)"
    )
    return any(
        str(item.get("role", "")).upper() == "USER"
        and pattern.search(str(item.get("content", ""))) is not None
        for item in messages
    )


def _bounded_lines(lines: Iterable[str], max_chars: int) -> str:
    result: list[str] = []
    remaining = max(1, max_chars)
    for line in lines:
        normalized = " ".join(str(line or "").split())
        if not normalized:
            continue
        separator = 1 if result else 0
        if remaining <= separator:
            break
        value = normalized[: remaining - separator]
        if value:
            result.append(value)
            remaining -= len(value) + separator
        if len(value) < len(normalized):
            break
    return "\n".join(result)
