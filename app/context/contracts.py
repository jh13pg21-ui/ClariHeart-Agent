"""Context Planner 的不可变输入、输出与审计契约。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.enums import RiskLevel


@dataclass(frozen=True)
class ContextSection:
    id: str
    category: str
    content: str
    priority: int = 50
    required: bool = False
    trust: str = "UNTRUSTED_DATA"
    sensitivity: RiskLevel = RiskLevel.LOW
    provenance_ids: tuple[str, ...] = ()
    loading_reason: str = ""
    message_role: str = ""
    token_count: int = 0
    compaction_level: int = 0

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("ContextSection.id 不能为空")
        if not self.category.strip():
            raise ValueError("ContextSection.category 不能为空")
        if not 0 <= self.priority <= 100:
            raise ValueError("ContextSection.priority 必须位于 [0, 100]")
        if self.message_role and self.message_role not in {"system", "user", "assistant"}:
            raise ValueError("ContextSection.message_role 非法")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextEnvelope:
    request_id: str
    agent_name: str
    task_name: str
    sections: tuple[ContextSection, ...]
    risk_level: RiskLevel = RiskLevel.LOW
    reactive: bool = False


@dataclass(frozen=True)
class CompactionAction:
    layer: str
    action: str
    section_ids: tuple[str, ...]
    reason: str
    tokens_before: int
    tokens_after: int


@dataclass(frozen=True)
class ContextPlan:
    request_id: str
    agent_name: str
    task_name: str
    sections: tuple[ContextSection, ...]
    actions: tuple[CompactionAction, ...]
    input_budget: int
    hard_limit: int
    target_tokens: int
    tokens_before: int
    tokens_after: int
    dropped_section_ids: tuple[str, ...]
    plan_hash: str
    reactive: bool = False

    @property
    def compacted(self) -> bool:
        return bool(self.dropped_section_ids or self.actions)
