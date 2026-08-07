from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import ChatMessage, ChatSession, LongTermMemory, UserAccount
from app.schemas.dtos import LongTermMemoryResponse, MemoryCandidate
from app.services.ai import AiClient
from app.services.privacy import PrivacySanitizer
from app.services.data_protection import SensitiveTextProtector
from app.services.memory_ranking import MemoryRanker
from app.prompts.runtime import registered_complete


MEMORY_TYPES = {
    "PROFILE",
    "PREFERENCE",
    "SUPPORT",
    "CONTEXT",
    "GOAL",
    "CONSTRAINT",
}
MEMORY_ACTIONS = {"CREATE", "CONFIRM", "SUPERSEDE", "CONFLICT", "IGNORE"}
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
        current = datetime.utcnow()
        return (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.status == "ACTIVE",
                or_(
                    LongTermMemory.expires_at.is_(None),
                    LongTermMemory.expires_at > current,
                ),
            )
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
                "memoryKey": item.memory_key,
                "name": item.name,
                "description": item.description,
                "status": item.status,
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
        semantic = await self._select_with_model(items, query, selected_limit)
        semantic_ids = {item.public_id for item in semantic}
        ranked = MemoryRanker(self.protector.reveal).rank(
            items,
            query,
            now=datetime.utcnow(),
            semantic_ids=semantic_ids,
        )
        return [item.memory for item in ranked if item.relevance > 0][:selected_limit]

    def mark_used(self, items: Iterable[LongTermMemory]) -> None:
        current = datetime.utcnow()
        for item in items:
            item.usage_count = int(item.usage_count or 0) + 1
            item.last_accessed_at = current
            self.db.add(item)

    def mark_used_ids(self, user_id: int, public_ids: Iterable[str]) -> None:
        ids = tuple(dict.fromkeys(str(value) for value in public_ids if value))
        if not ids:
            return
        items = (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.public_id.in_(ids),
                LongTermMemory.status == "ACTIVE",
            )
            .all()
        )
        self.mark_used(items)

    def upsert(
        self,
        user_id: int,
        source_session_id: int | None,
        memory_type: str,
        name: str,
        description: str,
        body: str,
    ) -> LongTermMemory | None:
        return self.upsert_candidate(
            user_id,
            source_session_id,
            MemoryCandidate(
                memory_type=memory_type,
                name=name,
                description=description,
                body=body,
                evidence_message_ids=(),
                confidence=0.5,
                extraction_method="legacy",
                prompt_version="legacy",
            ),
        )

    def upsert_candidate(
        self,
        user_id: int,
        source_session_id: int | None,
        candidate: MemoryCandidate,
    ) -> LongTermMemory | None:
        if not self._user_enabled(user_id):
            return None
        normalized = self._validate_candidate(
            candidate.memory_type,
            candidate.name,
            candidate.description,
            candidate.body,
        )
        if normalized is None:
            return None
        evidence_ids = tuple(
            dict.fromkeys(
                int(value)
                for value in candidate.evidence_message_ids
                if int(value) > 0
            )
        )
        if candidate.extraction_method != "legacy" and not self._valid_user_evidence(
            user_id,
            source_session_id,
            evidence_ids,
        ):
            return None
        normalized_type, safe_name, safe_description, safe_body = normalized
        action = str(candidate.action or "CREATE").strip().upper()
        if action not in MEMORY_ACTIONS:
            return None
        if action == "IGNORE":
            return None
        related_ids = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in candidate.related_memory_ids
                if str(value).strip()
            )
        )
        related = self._related_memories(user_id, related_ids)
        if len(related) != len(related_ids):
            return None
        memory_key = self._canonical_memory_key(
            normalized_type,
            safe_name,
            candidate.memory_key,
        )
        if related and not str(candidate.memory_key or "").strip():
            memory_key = related[0].memory_key or memory_key
        content_hash = hashlib.sha256(
            f"{normalized_type}\n{safe_body}".encode("utf-8")
        ).hexdigest()
        now = datetime.utcnow()
        exact = (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.content_hash == content_hash,
            )
            .first()
        )
        if exact is not None:
            existing_evidence = self._evidence_ids(exact)
            exact.evidence_message_ids_json = json.dumps(
                list(dict.fromkeys((*existing_evidence, *evidence_ids))),
                separators=(",", ":"),
            )
            exact.confirmation_count = int(exact.confirmation_count or 0) + 1
            exact.last_confirmed_at = now
            exact.confidence = max(
                float(exact.confidence or 0.0),
                self._bounded_confidence(candidate.confidence),
            )
            exact.updated_at = now
            exact.memory_key = exact.memory_key or memory_key
            exact.resolution_reason = self.privacy.sanitize(str(candidate.reason or ""))[:1000]
            if exact.status != "ACTIVE" and action != "CONFLICT":
                active_slot = self._active_slot(user_id, memory_key, exclude_id=exact.id)
                latest_version = max(
                    [int(exact.version or 1), *(int(item.version or 1) for item in active_slot)]
                )
                for item in active_slot:
                    item.status = "SUPERSEDED"
                    item.updated_at = now
                    self.db.add(item)
                exact.status = "ACTIVE"
                exact.version = latest_version + 1
                exact.supersedes_memory_id = active_slot[0].id if active_slot else None
                exact.conflict_group_id = None
            self.db.add(exact)
            return exact

        if action == "CONFIRM":
            target = next((item for item in related if item.status == "ACTIVE"), None)
            if target is None:
                return None
            target.evidence_message_ids_json = json.dumps(
                list(dict.fromkeys((*self._evidence_ids(target), *evidence_ids))),
                separators=(",", ":"),
            )
            target.confirmation_count = int(target.confirmation_count or 0) + 1
            target.last_confirmed_at = now
            target.confidence = max(
                float(target.confidence or 0.0),
                self._bounded_confidence(candidate.confidence),
            )
            target.resolution_reason = self.privacy.sanitize(str(candidate.reason or ""))[:1000]
            target.updated_at = now
            self.db.add(target)
            return target

        previous_rows = self._active_slot(user_id, memory_key)
        if action == "SUPERSEDE":
            by_id = {item.id: item for item in previous_rows}
            for item in related:
                if item.status == "ACTIVE":
                    by_id[item.id] = item
            previous_rows = sorted(
                by_id.values(),
                key=lambda item: (int(item.version or 1), item.id),
                reverse=True,
            )
        previous = previous_rows[0] if previous_rows else None
        if action == "CONFLICT":
            previous = None
        all_versions = (
            self.db.query(LongTermMemory.version)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.memory_key == memory_key,
            )
            .all()
        )
        latest_version = max((int(row[0] or 1) for row in all_versions), default=0)
        version = latest_version + 1
        if action != "CONFLICT":
            for item in previous_rows:
                item.status = "SUPERSEDED"
                item.updated_at = now
                self.db.add(item)

        conflict_group_id = None
        status = "ACTIVE"
        if action == "CONFLICT":
            status = "CONFLICTED"
            conflict_group_id = next(
                (item.conflict_group_id for item in related if item.conflict_group_id),
                uuid.uuid4().hex,
            )
            for item in related:
                item.conflict_group_id = conflict_group_id
                item.updated_at = now
                self.db.add(item)
        memory = LongTermMemory(
            public_id=uuid.uuid4().hex,
            user_id=user_id,
            source_session_id=source_session_id,
            memory_type=normalized_type,
            name=safe_name,
            description=safe_description,
            body=self.protector.protect(safe_body),
            content_hash=content_hash,
            status=status,
            evidence_message_ids_json=json.dumps(list(evidence_ids), separators=(",", ":")),
            confidence=self._bounded_confidence(candidate.confidence),
            extraction_method=str(candidate.extraction_method or "model")[:32],
            prompt_version=str(candidate.prompt_version or "memory_candidate_v3")[:64],
            model_provider=str(candidate.model_provider or "")[:32],
            model_name=str(candidate.model_name or "")[:128],
            version=version,
            usage_count=0,
            confirmation_count=0,
            supersedes_memory_id=previous.id if previous is not None else None,
            memory_key=memory_key,
            conflict_group_id=conflict_group_id,
            consolidated_from_ids_json="[]",
            resolution_reason=self.privacy.sanitize(str(candidate.reason or ""))[:1000],
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
        messages = [
            (row.id, row.role, self.protector.reveal(row.content))
            for row in reversed(rows)
        ]
        return await self.extract_from_messages(
            assistant.user_id,
            assistant.session_id,
            messages,
        )

    async def extract_from_messages(
        self,
        user_id: int,
        source_session_id: int | None,
        messages: Sequence[tuple[int, str, str] | tuple[str, str]],
    ) -> list[LongTermMemory]:
        if not getattr(self.settings, "long_term_memory_enabled", True) or not self._user_enabled(user_id):
            return []
        normalized_messages = self._normalize_source_messages(messages[-10:])
        allowed_user_ids = {
            message_id
            for message_id, role, _ in normalized_messages
            if message_id > 0 and role.upper() == "USER"
        }
        dialogue = [
            {
                "id": message_id,
                "role": role.upper(),
                "content": self.privacy.sanitize(content),
            }
            for message_id, role, content in normalized_messages
        ]
        memory_index = self.index_for_user(user_id)
        existing_index = json.dumps(memory_index, ensure_ascii=False)
        candidates: list[MemoryCandidate] = []
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
            provider, model = self._model_identity()
            candidates = self._parse_candidates(
                raw,
                allowed_user_ids,
                provider,
                model,
                {item["id"] for item in memory_index},
            )
        except Exception:
            candidates = []
        if not candidates:
            candidates = self._deterministic_candidates(normalized_messages)

        stored: list[LongTermMemory] = []
        for candidate in candidates[:5]:
            memory = self.upsert_candidate(
                user_id,
                source_session_id,
                candidate,
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
            status=item.status,
            confidence=float(item.confidence or 0.0),
            version=int(item.version or 1),
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
        messages: Sequence[tuple[int, str, str]],
    ) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        for message_id, role, content in messages:
            if role.upper() != "USER" or message_id <= 0:
                continue
            text = content.strip()
            match = re.search(r"(?:请)?记住[，,:：\s]*(.+)", text)
            if match:
                body = match.group(1).strip("。！! ")
                candidates.append(
                    MemoryCandidate(
                        "CONTEXT",
                        "学生明确要求记住",
                        body[:80],
                        body,
                        (message_id,),
                        0.75,
                        extraction_method="deterministic",
                    )
                )
                continue
            match = re.search(r"以后(?:请)?(?:叫我|称呼我)[，,:：\s]*(.+)", text)
            if match:
                name = match.group(1).strip("。！! ")
                candidates.append(
                    MemoryCandidate(
                        "PROFILE",
                        "称呼偏好",
                        f"学生希望被称为{name}",
                        f"学生希望以后被称为{name}。",
                        (message_id,),
                        0.9,
                        extraction_method="deterministic",
                    )
                )
                continue
            match = re.search(r"我(?:更)?喜欢[，,:：\s]*(.+)", text)
            if match:
                preference = match.group(1).strip("。！! ")
                candidates.append(
                    MemoryCandidate(
                        "PREFERENCE",
                        "学生偏好",
                        preference[:80],
                        f"学生喜欢{preference}。",
                        (message_id,),
                        0.8,
                        extraction_method="deterministic",
                    )
                )
        return candidates

    @staticmethod
    def _parse_candidates(
        raw: str,
        allowed_user_ids: set[int],
        provider: str,
        model: str,
        allowed_memory_ids: set[str] | None = None,
    ) -> list[MemoryCandidate]:
        try:
            data = json.loads(LongTermMemoryService._strip_code_fence(raw))
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        result: list[MemoryCandidate] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_ids = item.get("evidenceMessageIds")
            if not isinstance(raw_ids, list):
                continue
            evidence = tuple(
                dict.fromkeys(
                    int(value)
                    for value in raw_ids
                    if isinstance(value, int) and value in allowed_user_ids
                )
            )
            if not evidence:
                continue
            raw_related = item.get("relatedMemoryIds", [])
            if not isinstance(raw_related, list) or any(
                not isinstance(value, str) for value in raw_related
            ):
                continue
            related = tuple(dict.fromkeys(value.strip() for value in raw_related if value.strip()))
            if allowed_memory_ids is not None and any(
                value not in allowed_memory_ids for value in related
            ):
                continue
            action = str(item.get("action", "CREATE")).strip().upper()
            if action not in MEMORY_ACTIONS:
                continue
            result.append(
                MemoryCandidate(
                    memory_type=str(item.get("type", "")),
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    body=str(item.get("body", "")),
                    evidence_message_ids=evidence,
                    confidence=LongTermMemoryService._bounded_confidence(
                        item.get("confidence", 0.5)
                    ),
                    memory_key=str(item.get("memoryKey", "")),
                    action=action,
                    related_memory_ids=related,
                    reason=str(item.get("reason", "")),
                    extraction_method="model",
                    prompt_version="memory_candidate_v3",
                    model_provider=provider,
                    model_name=model,
                )
            )
        return result

    def _related_memories(
        self,
        user_id: int,
        public_ids: tuple[str, ...],
    ) -> list[LongTermMemory]:
        if not public_ids:
            return []
        rows = (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.public_id.in_(public_ids),
                LongTermMemory.status.in_(("ACTIVE", "CONFLICTED")),
            )
            .all()
        )
        by_public_id = {item.public_id: item for item in rows}
        return [by_public_id[value] for value in public_ids if value in by_public_id]

    def _active_slot(
        self,
        user_id: int,
        memory_key: str,
        exclude_id: int | None = None,
    ) -> list[LongTermMemory]:
        query = self.db.query(LongTermMemory).filter(
            LongTermMemory.user_id == user_id,
            LongTermMemory.memory_key == memory_key,
            LongTermMemory.status == "ACTIVE",
        )
        if exclude_id is not None:
            query = query.filter(LongTermMemory.id != exclude_id)
        return query.order_by(LongTermMemory.version.desc(), LongTermMemory.id.desc()).all()

    @staticmethod
    def _canonical_memory_key(memory_type: str, name: str, provided: str = "") -> str:
        source = str(provided or "").strip().lower()
        if not source:
            source = f"{str(memory_type or '').strip().lower()}.{str(name or '').strip().lower()}"
        normalized = re.sub(r"[^\w]+", ".", source, flags=re.UNICODE).strip(".")
        return normalized[:191] or f"{str(memory_type or 'memory').lower()}.unnamed"

    def _valid_user_evidence(
        self,
        user_id: int,
        source_session_id: int | None,
        evidence_ids: tuple[int, ...],
    ) -> bool:
        if not evidence_ids:
            return False
        query = self.db.query(ChatMessage.id).filter(
            ChatMessage.id.in_(evidence_ids),
            ChatMessage.user_id == user_id,
            ChatMessage.role == "USER",
        )
        if source_session_id is not None:
            query = query.filter(ChatMessage.session_id == source_session_id)
        return {int(row[0]) for row in query.all()} == set(evidence_ids)

    @staticmethod
    def _evidence_ids(memory: LongTermMemory) -> tuple[int, ...]:
        try:
            values = json.loads(memory.evidence_message_ids_json or "[]")
            return tuple(int(value) for value in values if int(value) > 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()

    @staticmethod
    def _bounded_confidence(value) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _normalize_source_messages(
        messages: Sequence[tuple[int, str, str] | tuple[str, str]],
    ) -> list[tuple[int, str, str]]:
        normalized: list[tuple[int, str, str]] = []
        for item in messages:
            if len(item) == 3:
                message_id, role, content = item
            else:
                role, content = item
                message_id = 0
            normalized.append((int(message_id), str(role), str(content)))
        return normalized

    def _model_identity(self) -> tuple[str, str]:
        provider = str(getattr(self.settings, "ai_provider", "mock")).lower()
        if provider == "ollama":
            return provider, str(getattr(self.settings, "ollama_model", ""))
        if provider == "openai":
            return provider, str(getattr(self.settings, "openai_model", ""))
        return provider, "mock"

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
