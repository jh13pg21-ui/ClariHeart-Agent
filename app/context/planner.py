"""确定性 L0-L3 Context Planner。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Mapping

from app.context.contracts import (
    CompactionAction,
    ContextEnvelope,
    ContextPlan,
    ContextSection,
)
from app.context.tokens import TokenEstimator, calculate_input_budget
from app.llm.capabilities import ModelCapabilities


class ContextPlanningError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


DEFAULT_CATEGORY_BUDGET_RATIOS = {
    "global": 1.0,
    "agent": 1.0,
    "task": 1.0,
    "user": 1.0,
    "conversation": 0.35,
    "rag": 0.25,
    "memory": 0.20,
    "skill": 0.15,
    "profile": 0.10,
}


class ContextPlanner:
    def __init__(
        self,
        estimator: TokenEstimator,
        *,
        reserve_tokens: int = 1024,
        margin_ratio: float = 0.10,
        category_budget_ratios: Mapping[str, float] | None = None,
    ) -> None:
        self.estimator = estimator
        self.reserve_tokens = reserve_tokens
        self.margin_ratio = margin_ratio
        self.category_budget_ratios = dict(
            category_budget_ratios or DEFAULT_CATEGORY_BUDGET_RATIOS
        )

    def plan(
        self,
        envelope: ContextEnvelope,
        capabilities: ModelCapabilities,
        requested_output_tokens: int,
    ) -> ContextPlan:
        input_budget = calculate_input_budget(
            capabilities,
            requested_output_tokens,
            reserve=self.reserve_tokens,
            margin_ratio=self.margin_ratio,
        )
        hard_limit = max(1, int(input_budget * 0.95))
        normalized = tuple(self._normalize(section) for section in envelope.sections)
        tokens_before = sum(section.token_count for section in normalized)
        deduplicated, dropped_duplicates = self._deduplicate(normalized)
        actions: list[CompactionAction] = []
        dropped: list[str] = list(dropped_duplicates)
        if dropped_duplicates:
            actions.append(
                CompactionAction(
                    layer="L0",
                    action="deduplicate",
                    section_ids=dropped_duplicates,
                    reason="相同分类、来源与内容仅保留首个 section",
                    tokens_before=tokens_before,
                    tokens_after=sum(item.token_count for item in deduplicated),
                )
            )

        required = tuple(section for section in deduplicated if section.required)
        required_tokens = sum(section.token_count for section in required)
        if required_tokens > hard_limit:
            raise ContextPlanningError(
                "INPUT_TOO_LARGE",
                "必需上下文已超过模型 hard limit，不能静默删除安全或当前输入",
            )

        working = deduplicated
        if sum(section.token_count for section in working) > hard_limit:
            working, conversation_drops = self._replace_old_conversation(working)
            if conversation_drops:
                dropped.extend(conversation_drops)
                actions.append(
                    CompactionAction(
                        layer="L2",
                        action="use_structured_summary",
                        section_ids=conversation_drops,
                        reason="结构化摘要替代较旧原始对话",
                        tokens_before=sum(item.token_count for item in deduplicated),
                        tokens_after=sum(item.token_count for item in working),
                    )
                )

        compacting = sum(section.token_count for section in working) > hard_limit
        target_tokens = int(input_budget * 0.70) if compacting else hard_limit
        optional = sorted(
            (section for section in working if not section.required),
            key=lambda section: (-section.priority, section.id),
        )
        selected = list(section for section in working if section.required)
        total = sum(section.token_count for section in selected)
        category_usage: dict[str, int] = {}
        budget_drops: list[str] = []
        for section in optional:
            ratio = self.category_budget_ratios.get(section.category, 0.10)
            category_limit = max(0, int(input_budget * ratio))
            category_total = category_usage.get(section.category, 0)
            if (
                total + section.token_count <= target_tokens
                and category_total + section.token_count <= category_limit
            ):
                selected.append(section)
                total += section.token_count
                category_usage[section.category] = category_total + section.token_count
            else:
                budget_drops.append(section.id)

        if budget_drops:
            dropped.extend(budget_drops)
            actions.append(
                CompactionAction(
                    layer="L3" if compacting else "L1",
                    action="priority_budget_selection",
                    section_ids=tuple(budget_drops),
                    reason=(
                        "超过 hard limit，按优先级压缩到 70% 水位"
                        if compacting
                        else "超过分类预算上限"
                    ),
                    tokens_before=sum(item.token_count for item in working),
                    tokens_after=total,
                )
            )

        if total > hard_limit:
            raise ContextPlanningError(
                "CONTEXT_UNRECOVERABLE",
                "确定性压缩后仍超过模型 hard limit",
            )
        selected_tuple = tuple(selected)
        plan_hash = self._plan_hash(
            envelope,
            selected_tuple,
            input_budget,
            tuple(dict.fromkeys(dropped)),
        )
        return ContextPlan(
            request_id=envelope.request_id,
            agent_name=envelope.agent_name,
            task_name=envelope.task_name,
            sections=selected_tuple,
            actions=tuple(actions),
            input_budget=input_budget,
            hard_limit=hard_limit,
            target_tokens=target_tokens,
            tokens_before=tokens_before,
            tokens_after=total,
            dropped_section_ids=tuple(dict.fromkeys(dropped)),
            plan_hash=plan_hash,
            reactive=envelope.reactive,
        )

    def _normalize(self, section: ContextSection) -> ContextSection:
        content = "\n".join(
            line.strip() for line in section.content.replace("\r\n", "\n").split("\n")
            if line.strip()
        )
        provenance = tuple(dict.fromkeys(item.strip() for item in section.provenance_ids if item.strip()))
        token_count = self.estimator.count(content) + 6
        return replace(
            section,
            id=section.id.strip(),
            category=section.category.strip().lower(),
            content=content,
            provenance_ids=provenance,
            token_count=token_count,
        )

    @staticmethod
    def _deduplicate(
        sections: tuple[ContextSection, ...],
    ) -> tuple[tuple[ContextSection, ...], tuple[str, ...]]:
        seen = set()
        kept = []
        dropped = []
        seen_ids = set()
        for section in sections:
            if section.id in seen_ids:
                raise ContextPlanningError(
                    "DUPLICATE_SECTION_ID",
                    f"Context section ID 重复: {section.id}",
                )
            seen_ids.add(section.id)
            key = (section.category, section.provenance_ids, section.content_hash)
            if key in seen:
                dropped.append(section.id)
                continue
            seen.add(key)
            kept.append(section)
        return tuple(kept), tuple(dropped)

    @staticmethod
    def _replace_old_conversation(
        sections: tuple[ContextSection, ...],
    ) -> tuple[tuple[ContextSection, ...], tuple[str, ...]]:
        if not any(section.id == "conversation.summary" for section in sections):
            return sections, ()
        dropped = tuple(
            section.id
            for section in sections
            if section.id.startswith("conversation.history.") and not section.required
        )
        return (
            tuple(section for section in sections if section.id not in set(dropped)),
            dropped,
        )

    @staticmethod
    def _plan_hash(
        envelope: ContextEnvelope,
        sections: tuple[ContextSection, ...],
        input_budget: int,
        dropped: tuple[str, ...],
    ) -> str:
        payload = json.dumps(
            {
                "requestId": envelope.request_id,
                "agent": envelope.agent_name,
                "task": envelope.task_name,
                "sections": [
                    {
                        "id": section.id,
                        "hash": section.content_hash,
                        "tokens": section.token_count,
                    }
                    for section in sections
                ],
                "inputBudget": input_budget,
                "dropped": dropped,
                "reactive": envelope.reactive,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
