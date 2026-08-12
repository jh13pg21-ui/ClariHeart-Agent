"""Claude Code 风格四层门控的长期记忆 Dream 整合服务。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.models.entities import (
    LongTermMemory,
    MemoryConsolidationRun,
    MemoryDreamState,
    UserAccount,
)
from app.prompts.runtime import registered_complete
from app.services.ai import AiClient
from app.services.data_protection import SensitiveTextProtector
from app.services.privacy import PrivacySanitizer


ALLOWED_ACTIONS = {"KEEP", "MERGE", "SUPERSEDE", "EXPIRE"}
ALLOWED_MEMORY_TYPES = {
    "PROFILE",
    "PREFERENCE",
    "SUPPORT",
    "CONTEXT",
    "GOAL",
    "CONSTRAINT",
}


@dataclass(frozen=True)
class ConsolidationDecision:
    action: str
    source_ids: tuple[str, ...]
    merged_memory: dict[str, str] | None = None


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

    def should_schedule(self, user_id: int, now: datetime | None = None) -> bool:
        current = now or datetime.utcnow()
        state = (
            self.db.query(MemoryDreamState)
            .filter(MemoryDreamState.user_id == user_id)
            .first()
        )
        return self._passes_gates(user_id, state, current)

    def reserve_schedule(
        self,
        user_id: int,
        now: datetime | None = None,
        trigger_reason: str = "threshold",
    ) -> MemoryConsolidationRun | None:
        current = now or datetime.utcnow()
        user_query = self.db.query(UserAccount).filter(UserAccount.id == user_id)
        if self.db.get_bind().dialect.name != "sqlite":
            user_query = user_query.with_for_update()
        user = user_query.first()
        if user is None or not user.long_term_memory_enabled:
            return None

        state = self._state_for_update(user_id)
        if not self._passes_base_gates(user_id, state, current):
            return None

        # 扫描节流在真正扫描候选前更新；即使内容阈值未满足，也不会高频扫库。
        state.last_scanned_at = current
        state.updated_at = current
        self.db.add(state)
        if not self._passes_content_gates(user_id, state, current):
            return None

        self._recover_stale_runs(user_id, state, current)
        active = self._active_memories(user_id)
        run = MemoryConsolidationRun(
            public_id=uuid.uuid4().hex,
            user_id=user_id,
            status="QUEUED",
            trigger_reason=str(trigger_reason or "threshold")[:64],
            new_memory_count=len(active),
            modified_session_count=len(
                {item.source_session_id for item in active if item.source_session_id}
            ),
            source_memory_ids_json=json.dumps(
                [item.public_id for item in active],
                separators=(",", ":"),
            ),
            created_at=current,
            updated_at=current,
        )
        self.db.add(run)
        self.db.flush()
        state.lease_owner = run.public_id
        state.lease_acquired_at = current
        state.lease_expires_at = current + self._lease_duration()
        state.last_error = ""
        state.updated_at = current
        self.db.add(state)
        self.db.flush()
        return run

    async def consolidate(
        self,
        user_id: int,
        run: MemoryConsolidationRun | None = None,
    ) -> ConsolidationResult:
        current = datetime.utcnow()
        scheduled = run is not None
        if run is None:
            run = MemoryConsolidationRun(
                public_id=uuid.uuid4().hex,
                user_id=user_id,
                status="RUNNING",
                trigger_reason="manual",
                created_at=current,
                updated_at=current,
            )
            self.db.add(run)
            self.db.flush()
        elif not self._claim_or_renew_lease(user_id, run, current):
            run.status = "STALE_LEASE"
            run.last_error = "Dream 租约已被其他运行接管"
            run.finished_at = current
            run.updated_at = current
            self.db.add(run)
            self.db.flush()
            return ConsolidationResult(
                status="STALE_LEASE",
                run_public_id=run.public_id,
                error=run.last_error,
            )

        run.status = "RUNNING"
        run.started_at = current
        run.updated_at = current
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
                            "memoryKey": item.memory_key,
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
            return self._fail_run(run, "FAILED", exc, release_lease=False)
        if decisions is None:
            return self._fail_run(
                run,
                "INVALID_OUTPUT",
                ValueError("整合输出未通过动作、来源 ID 或合并内容校验"),
                release_lease=True,
            )

        try:
            self._validate_merge_decisions(user_id, decisions)
        except (TypeError, ValueError) as exc:
            return self._fail_run(
                run,
                "INVALID_OUTPUT",
                exc,
                release_lease=True,
            )

        kept = merged = superseded = expired = 0
        now = datetime.utcnow()
        try:
            for decision in decisions:
                source_ids = decision.source_ids
                if decision.action == "KEEP":
                    kept += len(source_ids)
                    continue
                if decision.action == "MERGE":
                    sources = [by_id[source_id] for source_id in source_ids]
                    self._create_merged_memory(
                        user_id,
                        sources,
                        decision.merged_memory or {},
                        now,
                    )
                    for item in sources:
                        item.status = "SUPERSEDED"
                        item.updated_at = now
                        self.db.add(item)
                        superseded += 1
                    merged += max(0, len(source_ids) - 1)
                    continue
                if decision.action == "SUPERSEDE":
                    survivor = by_id[source_ids[0]]
                    survivor.confirmation_count = int(survivor.confirmation_count or 0) + max(
                        0, len(source_ids) - 1
                    )
                    survivor.updated_at = now
                    self.db.add(survivor)
                    for source_id in source_ids[1:]:
                        item = by_id[source_id]
                        item.status = "SUPERSEDED"
                        item.updated_at = now
                        self.db.add(item)
                        superseded += 1
                    continue
                for source_id in source_ids:
                    item = by_id[source_id]
                    item.status = "EXPIRED"
                    item.updated_at = now
                    self.db.add(item)
                    expired += 1
        except (TypeError, ValueError) as exc:
            return self._fail_run(
                run,
                "INVALID_OUTPUT",
                exc,
                release_lease=True,
            )

        result_payload = {
            "kept": kept,
            "merged": merged,
            "superseded": superseded,
            "expired": expired,
            "decisions": [self._decision_payload(item) for item in decisions],
        }
        run.status = "SUCCESS"
        run.result_json = json.dumps(result_payload, ensure_ascii=False, separators=(",", ":"))
        run.last_error = ""
        run.finished_at = now
        run.updated_at = now
        self.db.add(run)
        self._finish_dream_state(user_id, run, now, error="")
        self.db.flush()
        return ConsolidationResult(
            status="SUCCESS",
            kept=kept,
            merged=merged,
            superseded=superseded,
            expired=expired,
            run_public_id=run.public_id,
        )

    def _passes_gates(
        self,
        user_id: int,
        state: MemoryDreamState | None,
        now: datetime,
    ) -> bool:
        return self._passes_base_gates(user_id, state, now) and self._passes_content_gates(
            user_id, state, now
        )

    def _passes_base_gates(
        self,
        user_id: int,
        state: MemoryDreamState | None,
        now: datetime,
    ) -> bool:
        if not bool(getattr(self.settings, "memory_consolidation_enabled", False)):
            return False
        user = self.db.get(UserAccount, user_id)
        if user is None or not user.long_term_memory_enabled:
            return False

        last_consolidated = state.last_consolidated_at if state is not None else None
        last_success = (
            self.db.query(MemoryConsolidationRun.finished_at)
            .filter(
                MemoryConsolidationRun.user_id == user_id,
                MemoryConsolidationRun.status == "SUCCESS",
                MemoryConsolidationRun.finished_at.is_not(None),
            )
            .order_by(MemoryConsolidationRun.finished_at.desc())
            .first()
        )
        if last_success is not None and (
            last_consolidated is None or last_success[0] > last_consolidated
        ):
            last_consolidated = last_success[0]
        if last_consolidated is not None and now - last_consolidated < self._minimum_interval():
            return False

        if (
            state is not None
            and state.last_scanned_at is not None
            and now - state.last_scanned_at < self._scan_interval()
        ):
            return False
        if (
            state is not None
            and state.lease_owner
            and state.lease_expires_at is not None
            and state.lease_expires_at > now
        ):
            return False

        cutoff = now - self._lease_duration()
        live_run = (
            self.db.query(MemoryConsolidationRun.id)
            .filter(
                MemoryConsolidationRun.user_id == user_id,
                MemoryConsolidationRun.status.in_(("QUEUED", "RUNNING")),
                MemoryConsolidationRun.created_at > cutoff,
            )
            .first()
        )
        return live_run is None

    def _passes_content_gates(
        self,
        user_id: int,
        state: MemoryDreamState | None,
        now: datetime,
    ) -> bool:
        del now
        since = state.last_consolidated_at if state and state.last_consolidated_at else datetime.min
        active_query = self.db.query(LongTermMemory).filter(
            LongTermMemory.user_id == user_id,
            LongTermMemory.status == "ACTIVE",
            LongTermMemory.updated_at > since,
        )
        minimum_memories = max(
            1,
            int(getattr(self.settings, "memory_consolidation_min_active_memories", 10)),
        )
        if active_query.count() < minimum_memories:
            return False
        minimum_sessions = max(
            1,
            int(getattr(self.settings, "memory_consolidation_min_modified_sessions", 5)),
        )
        modified_sessions = (
            self.db.query(distinct(LongTermMemory.source_session_id))
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.status == "ACTIVE",
                LongTermMemory.updated_at > since,
                LongTermMemory.source_session_id.is_not(None),
            )
            .count()
        )
        return modified_sessions >= minimum_sessions

    def _state_for_update(self, user_id: int) -> MemoryDreamState:
        query = self.db.query(MemoryDreamState).filter(MemoryDreamState.user_id == user_id)
        if self.db.get_bind().dialect.name != "sqlite":
            query = query.with_for_update()
        state = query.first()
        if state is None:
            state = MemoryDreamState(user_id=user_id)
            self.db.add(state)
            self.db.flush()
        return state

    def _recover_stale_runs(
        self,
        user_id: int,
        state: MemoryDreamState,
        now: datetime,
    ) -> None:
        cutoff = now - self._lease_duration()
        stale_runs = (
            self.db.query(MemoryConsolidationRun)
            .filter(
                MemoryConsolidationRun.user_id == user_id,
                MemoryConsolidationRun.status.in_(("QUEUED", "RUNNING")),
            )
            .all()
        )
        for stale in stale_runs:
            if stale.public_id == state.lease_owner or stale.created_at <= cutoff:
                stale.status = "FAILED"
                stale.last_error = "Dream 租约过期，已由后续扫描恢复"
                stale.finished_at = now
                stale.updated_at = now
                self.db.add(stale)
        state.lease_owner = ""
        state.lease_acquired_at = None
        state.lease_expires_at = None

    def _claim_or_renew_lease(
        self,
        user_id: int,
        run: MemoryConsolidationRun,
        now: datetime,
    ) -> bool:
        state = self._state_for_update(user_id)
        if state.lease_owner and state.lease_owner != run.public_id:
            return False
        state.lease_owner = run.public_id
        state.lease_acquired_at = state.lease_acquired_at or now
        state.lease_expires_at = now + self._lease_duration()
        state.updated_at = now
        self.db.add(state)
        return True

    def _finish_dream_state(
        self,
        user_id: int,
        run: MemoryConsolidationRun,
        now: datetime,
        error: str,
    ) -> None:
        state = self._state_for_update(user_id)
        if state.lease_owner in ("", run.public_id):
            state.lease_owner = ""
            state.lease_acquired_at = None
            state.lease_expires_at = None
        state.last_scanned_at = now
        if not error:
            state.last_consolidated_at = now
        state.last_error = error
        state.updated_at = now
        self.db.add(state)

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

    def _create_merged_memory(
        self,
        user_id: int,
        sources: list[LongTermMemory],
        merged: dict[str, str],
        now: datetime,
    ) -> LongTermMemory:
        if len(sources) < 2:
            raise ValueError("MERGE 至少需要两条来源记忆")
        fields = self._validated_merged_fields(merged)
        normalized_type = fields["type"]
        memory_key = fields["memoryKey"]
        name = fields["name"]
        description = fields["description"]
        body = fields["body"]
        reason = fields["reason"]

        source_ids = [item.public_id for item in sources]
        evidence = list(
            dict.fromkeys(
                evidence_id
                for item in sources
                for evidence_id in self._evidence_ids(item)
            )
        )
        content_hash = fields["contentHash"]
        collision = (
            self.db.query(LongTermMemory.id)
            .filter(
                LongTermMemory.user_id == user_id,
                LongTermMemory.content_hash == content_hash,
            )
            .first()
        )
        if collision is not None:
            raise ValueError("MERGE 结果与已有记忆重复")
        latest_version = max(
            (
                int(row[0] or 1)
                for row in self.db.query(LongTermMemory.version)
                .filter(
                    LongTermMemory.user_id == user_id,
                    LongTermMemory.memory_key == memory_key,
                )
                .all()
            ),
            default=0,
        )
        memory = LongTermMemory(
            public_id=uuid.uuid4().hex,
            user_id=user_id,
            source_session_id=sources[-1].source_session_id,
            memory_type=normalized_type,
            memory_key=memory_key,
            name=name,
            description=description,
            body=self.protector.protect(body),
            content_hash=content_hash,
            status="ACTIVE",
            evidence_message_ids_json=json.dumps(evidence, separators=(",", ":")),
            confidence=max(float(item.confidence or 0.0) for item in sources),
            extraction_method="dream",
            prompt_version="memory_consolidation_v2",
            version=latest_version + 1,
            usage_count=sum(int(item.usage_count or 0) for item in sources),
            confirmation_count=sum(int(item.confirmation_count or 0) for item in sources)
            + len(sources)
            - 1,
            supersedes_memory_id=sources[0].id,
            consolidated_from_ids_json=json.dumps(source_ids, separators=(",", ":")),
            resolution_reason=reason,
            created_at=now,
            updated_at=now,
        )
        self.db.add(memory)
        self.db.flush()
        return memory

    def _validate_merge_decisions(
        self,
        user_id: int,
        decisions: list[ConsolidationDecision],
    ) -> None:
        content_hashes: set[str] = set()
        for decision in decisions:
            if decision.action != "MERGE":
                continue
            fields = self._validated_merged_fields(decision.merged_memory or {})
            content_hash = fields["contentHash"]
            if content_hash in content_hashes:
                raise ValueError("同一轮 Dream 产生了重复 MERGE 结果")
            collision = (
                self.db.query(LongTermMemory.id)
                .filter(
                    LongTermMemory.user_id == user_id,
                    LongTermMemory.content_hash == content_hash,
                )
                .first()
            )
            if collision is not None:
                raise ValueError("MERGE 结果与已有记忆重复")
            content_hashes.add(content_hash)

    def _validated_merged_fields(self, merged: dict[str, str]) -> dict[str, str]:
        normalized_type = str(merged.get("type", "")).strip().upper()
        memory_key = str(merged.get("memoryKey", "")).strip().lower()
        name = self.privacy.sanitize(str(merged.get("name", "")).strip())[:128]
        description = self.privacy.sanitize(str(merged.get("description", "")).strip())[:256]
        body = self.privacy.sanitize(str(merged.get("body", "")).strip())[:1000]
        reason = self.privacy.sanitize(str(merged.get("reason", "")).strip())[:1000]
        if normalized_type not in ALLOWED_MEMORY_TYPES:
            raise ValueError("MERGE 记忆类型非法")
        if not re.fullmatch(r"[a-z0-9_.-]{1,191}", memory_key):
            raise ValueError("MERGE memoryKey 非法")
        if not name or len(body) < 4:
            raise ValueError("MERGE 内容为空")
        raw = "\n".join((name, description, body))
        if any(pattern.search(raw) for pattern in self.privacy.patterns):
            raise ValueError("MERGE 内容包含敏感数据")
        return {
            "type": normalized_type,
            "memoryKey": memory_key,
            "name": name,
            "description": description,
            "body": body,
            "reason": reason,
            "contentHash": hashlib.sha256(
                f"{normalized_type}\n{body}".encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _parse_decisions(
        raw: str,
        allowed_ids: set[str],
    ) -> list[ConsolidationDecision] | None:
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
        decisions: list[ConsolidationDecision] = []
        consumed: set[str] = set()
        for item in raw_decisions:
            if not isinstance(item, dict):
                return None
            action = str(item.get("action", "")).upper()
            expected_keys = {"action", "sourceMemoryIds", "mergedMemory"} if action == "MERGE" else {
                "action",
                "sourceMemoryIds",
            }
            if set(item) != expected_keys or action not in ALLOWED_ACTIONS:
                return None
            source_values = item.get("sourceMemoryIds")
            if not isinstance(source_values, list):
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
            merged_memory = item.get("mergedMemory") if action == "MERGE" else None
            if action == "MERGE":
                required = {"type", "memoryKey", "name", "description", "body", "reason"}
                if not isinstance(merged_memory, dict) or set(merged_memory) != required:
                    return None
                if any(not isinstance(value, str) for value in merged_memory.values()):
                    return None
            consumed.update(source_ids)
            decisions.append(ConsolidationDecision(action, source_ids, merged_memory))
        return decisions

    def _fail_run(
        self,
        run: MemoryConsolidationRun,
        status: str,
        exc: BaseException,
        release_lease: bool,
    ) -> ConsolidationResult:
        error = self.privacy.sanitize(f"{type(exc).__name__}: {exc}")[:1000]
        now = datetime.utcnow()
        run.status = status
        run.last_error = error
        run.finished_at = now
        run.updated_at = now
        self.db.add(run)
        if release_lease:
            self._finish_dream_state(run.user_id, run, now, error=error)
        else:
            state = (
                self.db.query(MemoryDreamState)
                .filter(MemoryDreamState.user_id == run.user_id)
                .first()
            )
            if state is not None:
                state.last_error = error
                state.updated_at = now
                self.db.add(state)
        self.db.flush()
        return ConsolidationResult(
            status=status,
            run_public_id=run.public_id,
            error=error,
        )

    @staticmethod
    def _decision_payload(decision: ConsolidationDecision) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": decision.action,
            "sourceMemoryIds": list(decision.source_ids),
        }
        if decision.merged_memory is not None:
            payload["mergedMemory"] = decision.merged_memory
        return payload

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

    def _minimum_interval(self) -> timedelta:
        return timedelta(
            hours=max(
                0.0,
                float(getattr(self.settings, "memory_consolidation_min_interval_hours", 24)),
            )
        )

    def _scan_interval(self) -> timedelta:
        return timedelta(
            minutes=max(
                0.0,
                float(getattr(self.settings, "memory_consolidation_scan_interval_minutes", 60)),
            )
        )

    def _lease_duration(self) -> timedelta:
        return timedelta(
            seconds=max(
                60,
                int(getattr(self.settings, "memory_consolidation_lease_seconds", 3600)),
            )
        )
