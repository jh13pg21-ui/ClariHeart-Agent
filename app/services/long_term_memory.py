from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatMessage, ChatSession, LongTermMemory, UserAccount
from app.schemas.dtos import LongTermMemoryResponse
from app.services.ai import AiClient
from app.services.privacy import PrivacySanitizer
from app.services.data_protection import SensitiveTextProtector
from app.prompts.runtime import registered_complete


MEMORY_TYPES = {"PROFILE", "PREFERENCE", "SUPPORT", "CONTEXT"}
STABLE_MEMORY_SIGNALS = (
    "叫我",
    "称呼我",
    "我喜欢",
    "我不喜欢",
    "我偏好",
    "我希望以后",
    "我的目标",
    "长期计划",
    "正在准备",
)
HIGH_RISK_TERMS = (
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


def _has_stable_memory_signal(text: str) -> bool:
    normalized = (text or "").lower().replace(" ", "")
    return any(signal in normalized for signal in STABLE_MEMORY_SIGNALS)
DIAGNOSIS_TERMS = (
    "确诊",
    "诊断为",
    "抑郁症",
    "焦虑症",
    "双相",
    "精神分裂",
    "风险等级",
    "HIGH",
    "MEDIUM",
)


class LongTermMemoryService:
    """按用户隔离的跨会话长期记忆。

    结构对应 s09_memory：数据库行相当于单个 memory 文件，列表接口相当于
    MEMORY.md 索引，select_relevant 负责按当前问题选择少量全文，后台提取
    相当于 stop hook。相同类型和名称的记忆持续更新，避免无限堆积。
    """

    def __init__(
        self,
        db: Session,
        settings: Settings,
        ai: AiClient | None = None,
    ):
        self.db = db
        self.settings = settings
        self.ai = ai or AiClient(settings)
        self.privacy = PrivacySanitizer()
        self.protector = SensitiveTextProtector(settings)

    def should_schedule_extraction(
        self,
        session: ChatSession,
        assistant_message: ChatMessage,
    ) -> bool:
        if not self._user_enabled(session.user_id):
            return False
        latest_user = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session.id,
                ChatMessage.role == "USER",
                ChatMessage.id < assistant_message.id,
            )
            .order_by(ChatMessage.id.desc())
            .first()
        )
        if latest_user and _has_stable_memory_signal(latest_user.content):
            return True
        watermark = session.long_term_memory_extracted_message_id or 0
        new_message_count = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == session.id, ChatMessage.id > watermark)
            .count()
        )
        return new_message_count >= max(
            1,
            int(getattr(self.settings, "long_term_memory_extract_min_new_messages", 6)),
        )

    def list_for_user(self, user_id: int) -> list[LongTermMemory]:
        limit = max(1, int(getattr(self.settings, "long_term_memory_max_items", 200)))
        return (
            self.db.query(LongTermMemory)
            .filter(LongTermMemory.user_id == user_id)
            .order_by(LongTermMemory.updated_at.desc(), LongTermMemory.id.desc())
            .limit(limit)
            .all()
        )

    def responses_for_user(self, user_id: int) -> list[LongTermMemoryResponse]:
        return [self.to_response(item) for item in self.list_for_user(user_id)]

    def index_for_user(self, user_id: int) -> list[dict[str, str]]:
        return [
            {
                "id": item.public_id,
                "type": item.memory_type,
                "name": item.name,
                "description": item.description,
            }
            for item in self.list_for_user(user_id)
        ]

    async def select_relevant(
        self,
        user_id: int,
        query: str,
        limit: int | None = None,
    ) -> list[LongTermMemory]:
        if not getattr(self.settings, "long_term_memory_enabled", True) or not self._user_enabled(user_id):
            return []
        items = self.list_for_user(user_id)
        if not items:
            return []
        selected_limit = max(
            1,
            int(
                limit
                or getattr(self.settings, "long_term_memory_relevant_items", 5)
            ),
        )
        selected = await self._select_with_model(items, query, selected_limit)
        if not selected:
            selected = self._select_by_keywords(items, query, selected_limit)
        now = datetime.utcnow()
        for item in selected:
            item.last_accessed_at = now
            self.db.add(item)
        return selected

    def upsert(
        self,
        user_id: int,
        source_session_id: int | None,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> LongTermMemory | None:
        if not self._user_enabled(user_id):
            return None
        normalized = self._validate_candidate(
            memory_type,
            name,
            description,
            body,
        )
        if normalized is None:
            return None
        normalized_type, safe_name, safe_description, safe_body = normalized
        content_hash = hashlib.sha256(
            f"{normalized_type}\n{safe_body}".encode("utf-8")
        ).hexdigest()
        existing = (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.content_hash == content_hash,
            )
            .first()
        )
        if existing is None:
            existing = (
                self.db.query(LongTermMemory)
                .filter(
                    LongTermMemory.user_id == user_id,
                    LongTermMemory.memory_type == normalized_type,
                    LongTermMemory.name == safe_name,
                )
                .first()
            )
        if existing is not None:
            existing.source_session_id = source_session_id or existing.source_session_id
            existing.description = safe_description
            existing.body = self.protector.protect(safe_body)
            existing.content_hash = content_hash
            existing.updated_at = datetime.utcnow()
            self.db.add(existing)
            return existing
        memory = LongTermMemory(
            public_id=uuid.uuid4().hex,
            user_id=user_id,
            source_session_id=source_session_id,
            memory_type=normalized_type,
            name=safe_name,
            description=safe_description,
            body=self.protector.protect(safe_body),
            content_hash=content_hash,
        )
        self.db.add(memory)
        self.db.flush()
        return memory

    def delete_for_user(self, user_id: int, public_id: str) -> bool:
        memory = (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.public_id == public_id,
            )
            .first()
        )
        if memory is None:
            return False
        self.db.delete(memory)
        self.db.commit()
        return True

    def delete_all_for_user(self, user_id: int) -> int:
        removed = (
            self.db.query(LongTermMemory)
            .filter(LongTermMemory.user_id == user_id)
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return int(removed)

    async def extract_from_assistant_message(
        self,
        assistant_message_id: int,
    ) -> list[LongTermMemory]:
        assistant = self.db.get(ChatMessage, assistant_message_id)
        if assistant is None or assistant.role.upper() != "ASSISTANT":
            return []
        limit = max(
            2,
            int(getattr(self.settings, "long_term_memory_extract_messages", 10)),
        )
        rows = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.session_id == assistant.session_id,
                ChatMessage.id <= assistant.id,
            )
            .order_by(ChatMessage.id.desc())
            .limit(limit)
            .all()
        )
        messages = [(row.role, self.protector.reveal(row.content)) for row in reversed(rows)]
        return await self.extract_from_messages(
            assistant.user_id,
            assistant.session_id,
            messages,
        )

    async def extract_from_messages(
        self,
        user_id: int,
        source_session_id: int | None,
        messages: Sequence[tuple[str, str]],
    ) -> list[LongTermMemory]:
        if not getattr(self.settings, "long_term_memory_enabled", True) or not self._user_enabled(user_id):
            return []
        dialogue = "\n".join(
            f"{role.upper()}: {self.privacy.sanitize(content)}"
            for role, content in messages[-10:]
        )
        existing_index = json.dumps(
            self.index_for_user(user_id),
            ensure_ascii=False,
        )
        candidates: list[dict[str, str]] = []
        try:
            raw = await registered_complete(
                self.ai,
                agent_name="LongTermMemoryService",
                agent_prompt_id="agent.context",
                task_name="memory_extraction",
                payload={
                    "existingMemoryIndex": json.loads(existing_index),
                    "recentDialogue": dialogue,
                },
            )
            candidates = self._parse_candidates(raw)
        except Exception:
            candidates = []
        if not candidates:
            candidates = self._deterministic_candidates(messages)

        stored: list[LongTermMemory] = []
        for candidate in candidates[:5]:
            memory = self.upsert(
                user_id,
                source_session_id,
                candidate.get("type", ""),
                candidate.get("name", ""),
                candidate.get("description", ""),
                candidate.get("body", ""),
            )
            if memory is not None:
                stored.append(memory)
        return stored

    def format_for_prompt(self, items: Iterable[LongTermMemory]) -> str:
        formatted = [
            f"- [{item.memory_type}] {item.name}：{self.protector.reveal(item.body)}"
            for item in items
        ]
        return "\n".join(formatted) if formatted else "无相关长期记忆。"

    def to_response(self, item: LongTermMemory) -> LongTermMemoryResponse:
        return LongTermMemoryResponse(
            id=item.public_id,
            type=item.memory_type,
            name=item.name,
            description=item.description,
            body=self.protector.reveal(item.body),
            createdAt=item.created_at,
            updatedAt=item.updated_at,
        )

    async def _select_with_model(
        self,
        items: list[LongTermMemory],
        query: str,
        limit: int,
    ) -> list[LongTermMemory]:
        index = [
            {
                "id": item.public_id,
                "type": item.memory_type,
                "name": item.name,
                "description": item.description,
            }
            for item in items
        ]
        try:
            raw = await registered_complete(
                self.ai,
                agent_name="LongTermMemoryService",
                agent_prompt_id="agent.context",
                task_name="memory_selection",
                payload={
                    "limit": limit,
                    "currentInput": query,
                    "memoryIndex": index,
                },
            )
            ids = json.loads(self._strip_code_fence(raw))
            if not isinstance(ids, list):
                return []
            by_id = {item.public_id: item for item in items}
            return [
                by_id[item_id]
                for item_id in ids[:limit]
                if isinstance(item_id, str) and item_id in by_id
            ]
        except Exception:
            return []

    def _select_by_keywords(
        self,
        items: list[LongTermMemory],
        query: str,
        limit: int,
    ) -> list[LongTermMemory]:
        query_tokens = self._tokens(query)
        scored = []
        for item in items:
            text = f"{item.name} {item.description} {self.protector.reveal(item.body)}"
            score = len(query_tokens & self._tokens(text))
            if score:
                scored.append((score, item.updated_at, item.id, item))
        scored.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
        return [value[-1] for value in scored[:limit]]

    def _validate_candidate(
        self,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> tuple[str, str, str, str] | None:
        normalized_type = str(memory_type or "").strip().upper()
        raw = "\n".join((str(name or ""), str(description or ""), str(body or "")))
        if normalized_type not in MEMORY_TYPES or not str(body or "").strip():
            return None
        if any(pattern.search(raw) for pattern in self.privacy.patterns):
            return None
        lowered = raw.lower()
        if any(term.lower() in lowered for term in (*HIGH_RISK_TERMS, *DIAGNOSIS_TERMS)):
            return None
        safe_name = self.privacy.sanitize(str(name).strip())[:128]
        safe_description = self.privacy.sanitize(str(description).strip())[:256]
        safe_body = self.privacy.sanitize(str(body).strip())[:1000]
        if not safe_name or len(safe_body) < 4:
            return None
        return normalized_type, safe_name, safe_description, safe_body

    def _deterministic_candidates(
        self,
        messages: Sequence[tuple[str, str]],
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        for role, content in messages:
            if role.upper() != "USER":
                continue
            text = content.strip()
            match = re.search(r"(?:请)?记住[，,:：\s]*(.+)", text)
            if match:
                body = match.group(1).strip("。！! ")
                candidates.append(
                    {
                        "type": "CONTEXT",
                        "name": "学生明确要求记住",
                        "description": body[:80],
                        "body": body,
                    }
                )
                continue
            match = re.search(r"以后(?:请)?(?:叫我|称呼我)[，,:：\s]*(.+)", text)
            if match:
                name = match.group(1).strip("。！! ")
                candidates.append(
                    {
                        "type": "PROFILE",
                        "name": "称呼偏好",
                        "description": f"学生希望被称为{name}",
                        "body": f"学生希望以后被称为{name}。",
                    }
                )
                continue
            match = re.search(r"我(?:更)?喜欢[，,:：\s]*(.+)", text)
            if match:
                preference = match.group(1).strip("。！! ")
                candidates.append(
                    {
                        "type": "PREFERENCE",
                        "name": "学生偏好",
                        "description": preference[:80],
                        "body": f"学生喜欢{preference}。",
                    }
                )
        return candidates

    @staticmethod
    def _parse_candidates(raw: str) -> list[dict[str, str]]:
        try:
            data = json.loads(LongTermMemoryService._strip_code_fence(raw))
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _strip_code_fence(raw: str) -> str:
        value = str(raw or "").strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
            value = re.sub(r"\s*```$", "", value)
        return value.strip()

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = re.sub(r"\s+", "", str(text or "").lower())
        latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        grams = {
            chinese[index:index + 2]
            for index in range(max(0, len(chinese) - 1))
        }
        return latin | grams

    def _user_enabled(self, user_id: int) -> bool:
        user = self.db.get(UserAccount, user_id)
        return bool(user is not None and user.long_term_memory_enabled)
