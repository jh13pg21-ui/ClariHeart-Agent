"""低频、证据白名单约束的长期记忆整合服务。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.models.entities import LongTermMemory, MemoryConsolidationRun, UserAccount
from app.services.ai import AiClient
from app.services.data_protection import SensitiveTextProtector
from app.services.privacy import PrivacySanitizer
from app.prompts.runtime import registered_complete


ALLOWED_ACTIONS = {"KEEP", "MERGE", "SUPERSEDE", "EXPIRE"}


@dataclass(frozen=True)
class ConsolidationResult:
    status: str
    kept: int = 0
    merged: int = 0
    superseded: int = 0
    expired: int = 0
    run_public_id: str = ""
    error: str = ""


class MemoryConsolidationService:
    def __init__(self, db: Session, settings, ai: AiClient | None = None) -> None:
        self.db = db
        self.settings = settings
        self.ai = ai or AiClient(settings)
        self.privacy = PrivacySanitizer()
        self.protector = SensitiveTextProtector(settings)

    def should_schedule(self, user_id: int) -> bool:
        if not bool(getattr(self.settings, "memory_consolidation_enabled", False)):
            return False
        user = self.db.get(UserAccount, user_id)
        if user is None or not user.long_term_memory_enabled:
            return False
        lock = (
            self.db.query(MemoryConsolidationRun.id)
            .filter(
                MemoryConsolidationRun.user_id == user_id,
                MemoryConsolidationRun.status.in_(("QUEUED", "RUNNING")),
            )
            .first()
        )
        if lock is not None:
            return False
        last_success = (
            self.db.query(MemoryConsolidationRun)
            .filter(
                MemoryConsolidationRun.user_id == user_id,
                MemoryConsolidationRun.status == "SUCCESS",
            )
            .order_by(
                MemoryConsolidationRun.finished_at.desc(),
                MemoryConsolidationRun.id.desc(),
            )
            .first()
        )
        now = datetime.utcnow()
        minimum_interval = timedelta(
            hours=max(
                0.0,
                float(
                    getattr(
                        self.settings,
                        "memory_consolidation_min_interval_hours",
                        24,
                    )
                ),
            )
        )
        if (
            last_success is not None
            and last_success.finished_at is not None
            and now - last_success.finished_at < minimum_interval
        ):
            return False
        since = (
            last_success.finished_at
            if last_success is not None and last_success.finished_at is not None
            else datetime.min
        )
        active_query = self.db.query(LongTermMemory).filter(
            LongTermMemory.user_id == user_id,
            LongTermMemory.status == "ACTIVE",
            LongTermMemory.updated_at > since,
        )
        minimum_memories = max(
            1,
            int(
                getattr(
                    self.settings,
                    "memory_consolidation_min_active_memories",
                    10,
                )
            ),
        )
        if active_query.count() < minimum_memories:
            return False
        minimum_sessions = max(
            1,
            int(
                getattr(
                    self.settings,
                    "memory_consolidation_min_modified_sessions",
                    5,
                )
            ),
        )
        sessions = (
            self.db.query(distinct(LongTermMemory.source_session_id))
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.status == "ACTIVE",
                LongTermMemory.updated_at > since,
                LongTermMemory.source_session_id.is_not(None),
            )
            .count()
        )
        return sessions >= minimum_sessions

    def reserve_schedule(self, user_id: int) -> MemoryConsolidationRun | None:
        user_query = self.db.query(UserAccount).filter(UserAccount.id == user_id)
        if self.db.get_bind().dialect.name != "sqlite":
            user_query = user_query.with_for_update()
        if user_query.first() is None or not self.should_schedule(user_id):
            return None
        active = self._active_memories(user_id)
        run = MemoryConsolidationRun(
            public_id=uuid.uuid4().hex,
            user_id=user_id,
            status="QUEUED",
            trigger_reason="threshold",
            new_memory_count=len(active),
            modified_session_count=len(
                {item.source_session_id for item in active if item.source_session_id}
            ),
            source_memory_ids_json=json.dumps(
                [item.public_id for item in active],
                separators=(",", ":"),
            ),
        )
        self.db.add(run)
        self.db.flush()
        return run

    async def consolidate(
        self,
        user_id: int,
        run: MemoryConsolidationRun | None = None,
    ) -> ConsolidationResult:
        if run is None:
            run = MemoryConsolidationRun(
                public_id=uuid.uuid4().hex,
                user_id=user_id,
                status="RUNNING",
                trigger_reason="manual",
            )
            self.db.add(run)
            self.db.flush()
        run.status = "RUNNING"
        run.started_at = datetime.utcnow()
        run.updated_at = datetime.utcnow()
        self.db.add(run)
        memories = self._active_memories(user_id)
        by_id = {item.public_id: item for item in memories}
        run.source_memory_ids_json = json.dumps(list(by_id), separators=(",", ":"))
        try:
            raw = await registered_complete(
                self.ai,
                agent_name="MemoryConsolidationService",
                agent_prompt_id="agent.context",
                task_name="memory_consolidation",
                payload={
                    "memoryIndex": [
                        {
                            "id": item.public_id,
                            "type": item.memory_type,
                            "name": item.name,
                            "description": item.description,
                            "body": self.protector.reveal(item.body),
                            "confidence": float(item.confidence or 0.0),
                            "evidenceMessageIds": self._evidence_ids(item),
                            "version": int(item.version or 1),
                        }
                        for item in memories
                    ]
                },
            )
            decisions = self._parse_decisions(raw, set(by_id))
        except Exception as exc:
            return self._fail_run(run, "FAILED", exc)
        if decisions is None:
            return self._fail_run(
                run,
                "INVALID_OUTPUT",
                ValueError("整合输出未通过 action/sourceMemoryIds 白名单校验"),
            )

        kept = merged = superseded = expired = 0
        now = datetime.utcnow()
        for action, source_ids in decisions:
            if action == "KEEP":
                kept += len(source_ids)
                continue
            if action in {"MERGE", "SUPERSEDE"}:
                survivor = by_id[source_ids[0]]
                survivor.confirmation_count = int(survivor.confirmation_count or 0) + max(
                    0,
                    len(source_ids) - 1,
                )
                survivor.updated_at = now
                self.db.add(survivor)
                for source_id in source_ids[1:]:
                    item = by_id[source_id]
                    item.status = "SUPERSEDED"
                    item.updated_at = now
                    self.db.add(item)
                    superseded += 1
                if action == "MERGE":
                    merged += max(0, len(source_ids) - 1)
                continue
            for source_id in source_ids:
                item = by_id[source_id]
                item.status = "EXPIRED"
                item.updated_at = now
                self.db.add(item)
                expired += 1

        result_payload = {
            "kept": kept,
            "merged": merged,
            "superseded": superseded,
            "expired": expired,
            "decisions": [
                {"action": action, "sourceMemoryIds": list(source_ids)}
                for action, source_ids in decisions
            ],
        }
        run.status = "SUCCESS"
        run.result_json = json.dumps(result_payload, separators=(",", ":"))
        run.last_error = ""
        run.finished_at = now
        run.updated_at = now
        self.db.add(run)
        self.db.flush()
        return ConsolidationResult(
            status="SUCCESS",
            kept=kept,
            merged=merged,
            superseded=superseded,
            expired=expired,
            run_public_id=run.public_id,
        )

    def _active_memories(self, user_id: int) -> list[LongTermMemory]:
        return (
            self.db.query(LongTermMemory)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.status == "ACTIVE",
            )
            .order_by(LongTermMemory.updated_at.asc(), LongTermMemory.id.asc())
            .all()
        )

    @staticmethod
    def _parse_decisions(
        raw: str,
        allowed_ids: set[str],
    ) -> list[tuple[str, tuple[str, ...]]] | None:
        try:
            value = str(raw or "").strip()
            if value.startswith("```"):
                value = value.removeprefix("```json").removeprefix("```")
                value = value.removesuffix("```").strip()
            payload = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"decisions"}:
            return None
        raw_decisions = payload.get("decisions")
        if not isinstance(raw_decisions, list):
            return None
        decisions: list[tuple[str, tuple[str, ...]]] = []
        consumed: set[str] = set()
        for item in raw_decisions:
            if not isinstance(item, dict) or set(item) != {"action", "sourceMemoryIds"}:
                return None
            action = str(item.get("action", "")).upper()
            source_values = item.get("sourceMemoryIds")
            if action not in ALLOWED_ACTIONS or not isinstance(source_values, list):
                return None
            source_ids = tuple(
                dict.fromkeys(value for value in source_values if isinstance(value, str))
            )
            if not source_ids or any(value not in allowed_ids for value in source_ids):
                return None
            if consumed.intersection(source_ids):
                return None
            if action in {"MERGE", "SUPERSEDE"} and len(source_ids) < 2:
                return None
            consumed.update(source_ids)
            decisions.append((action, source_ids))
        return decisions

    def _fail_run(
        self,
        run: MemoryConsolidationRun,
        status: str,
        exc: BaseException,
    ) -> ConsolidationResult:
        error = self.privacy.sanitize(f"{type(exc).__name__}: {exc}")[:1000]
        run.status = status
        run.last_error = error
        run.finished_at = datetime.utcnow()
        run.updated_at = datetime.utcnow()
        self.db.add(run)
        self.db.flush()
        return ConsolidationResult(
            status=status,
            run_public_id=run.public_id,
            error=error,
        )

    @staticmethod
    def _evidence_ids(memory: LongTermMemory) -> list[int]:
        try:
            return [
                int(value)
                for value in json.loads(memory.evidence_message_ids_json or "[]")
                if int(value) > 0
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
