from __future__ import annotations

from app.core.enums import IntentType, RiskLevel
from app.graph.state import AgentState
from app.services.output_safety import OutputSafetyStatus


def route_context(state: AgentState) -> str:
    intent = str(((state.get("intent") or {}).get("payload") or {}).get("intent", IntentType.CHAT.value)).upper()
    risk = str(((state.get("risk") or {}).get("payload") or {}).get("risk", RiskLevel.LOW.value)).upper()
    return "gather_context" if intent == IntentType.CONSULT.value or risk in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value} else "skip_context"


def route_generation(state: AgentState) -> str:
    route = str(state.get("generation_route", "REVIEW")).upper()
    return {
        "COMPACT": "compact_context",
        "FALLBACK": "safe_fallback",
    }.get(route, "review_response")


def route_review(state: AgentState) -> str:
    review = state.get("output_safety") or {}
    status = str((review.get("payload") or {}).get("status", "")).upper()
    if status == OutputSafetyStatus.APPROVED.value:
        return "finalize"
    if status == OutputSafetyStatus.REVISE.value and int(state.get("revision_count", 0)) <= 1:
        return "generate_response"
    return "safe_fallback"
