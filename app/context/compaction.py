"""一次性 reactive context plan 与无正文审计记录。"""

from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy.orm import Session

from app.context.contracts import (
    CompactionAction,
    ContextEnvelope,
    ContextPlan,
)
from app.context.planner import ContextPlanner, ContextPlanningError
from app.llm.capabilities import ModelCapabilities
from app.models.entities import ContextCompactionRecord


EMERGENCY_SECTION_ALLOWLIST = (
    "global.identity",
    "global.safety",
    "agent.contract",
    "conversation.summary",
    "conversation.latest_turn",
    "skill.mandatory",
    "rag.top1",
    "user.current",
)


class CompactionEngine:
    def __init__(self, planner: ContextPlanner) -> None:
        self.planner = planner

    def reactive_plan(
        self,
        envelope: ContextEnvelope,
        capabilities: ModelCapabilities,
    ) -> ContextPlan:
        if envelope.reactive:
            raise ContextPlanningError(
                "CONTEXT_UNRECOVERABLE",
                "reactive context plan 每个请求最多执行一次",
            )
        by_id = {section.id: section for section in envelope.sections}
        selected = tuple(
            replace(by_id[section_id], required=True, priority=100)
            for section_id in EMERGENCY_SECTION_ALLOWLIST
            if section_id in by_id
        )
        removed = tuple(
            section.id
            for section in envelope.sections
            if section.id not in EMERGENCY_SECTION_ALLOWLIST
        )
        reactive_envelope = replace(
            envelope,
            sections=selected,
            reactive=True,
        )
        try:
            planned = self.planner.plan(
                reactive_envelope,
                capabilities,
                capabilities.default_output_tokens,
            )
        except ContextPlanningError as exc:
            raise ContextPlanningError(
                "CONTEXT_UNRECOVERABLE",
                f"应急 allowlist 仍无法满足模型窗口: {exc}",
            ) from exc
        full_tokens = sum(
            self.planner.estimator.count(section.content) + 6
            for section in envelope.sections
        )
        emergency_action = CompactionAction(
            layer="REACTIVE",
            action="emergency_allowlist",
            section_ids=removed,
            reason="Provider 返回 PROMPT_TOO_LONG，仅保留一次性应急 allowlist",
            tokens_before=full_tokens,
            tokens_after=planned.tokens_after,
        )
        dropped = tuple(dict.fromkeys((*removed, *planned.dropped_section_ids)))
        return replace(
            planned,
            actions=(emergency_action, *planned.actions),
            tokens_before=full_tokens,
            dropped_section_ids=dropped,
            reactive=True,
            reason="prompt_too_long",
            status="REACTIVE_PLANNED",
            watermark=planned.plan_hash,
        )


def record_compaction(
    db: Session,
    plan: ContextPlan,
    manifest_hash: str,
) -> ContextCompactionRecord:
    layers = [
        {
            "layer": action.layer,
            "action": action.action,
            "sectionIds": list(action.section_ids),
            "reason": action.reason,
            "tokensBefore": action.tokens_before,
            "tokensAfter": action.tokens_after,
        }
        for action in plan.actions
    ]
    record = ContextCompactionRecord(
        request_id=plan.request_id,
        session_id=plan.session_id,
        agent_name=plan.agent_name,
        task_name=plan.task_name,
        provider=plan.provider,
        model=plan.model,
        tokens_before=plan.tokens_before,
        tokens_after=plan.tokens_after,
        input_budget=plan.input_budget,
        reason=plan.reason or "planned_compaction",
        layers_json=json.dumps(layers, ensure_ascii=False, separators=(",", ":")),
        watermark=plan.watermark or plan.plan_hash,
        status=plan.status,
        manifest_hash=manifest_hash,
    )
    db.add(record)
    db.flush()
    return record
