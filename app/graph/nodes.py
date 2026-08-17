from __future__ import annotations

import asyncio
import copy
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from langgraph.runtime import Runtime

from app.agents.autonomous import (
    AgentNodeContext,
    AgentRuntimeServices,
    ContextAgent,
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
)
from app.agents.events import (
    AgentArtifact,
    AgentEventType,
    AgentNodeResult,
)
from app.graph.errors import AgentFailureKind, classify_agent_error
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.graph.artifacts import (
    artifact_checkpoint_summary,
    artifact_from_state,
    artifact_to_state,
    event_from_state,
    event_to_state,
)
from app.graph.state import AgentState
from app.services.ai import has_consult_signal
from app.services.assessment import PsychologyAssessment, heuristic
from app.services.output_safety import OutputSafetyStatus, safe_fallback
from app.services.risk_rules import detect_risk_signal
from app.services.runtime_metrics import get_runtime_metrics


DOMAIN_EVENT_TYPES = {
    AgentEventType.SAFETY_OVERRIDE,
    AgentEventType.REVISION_REQUESTED,
    AgentEventType.FINAL_ACCEPTED,
    AgentEventType.FALLBACK_APPLIED,
    AgentEventType.CLOUD_EGRESS_BLOCKED,
    AgentEventType.CONTEXT_COMPACTED,
    AgentEventType.CONTEXT_UNRECOVERABLE,
}


@dataclass
class GraphRuntimeContext:
    services: AgentRuntimeServices
    node_timeout_seconds: float
    node_max_attempts: int

    def agent(self, name: str, *, services: AgentRuntimeServices | None = None):
        agents = {
            "UnderstandingAgent": UnderstandingAgent,
            "SafetyAgent": SafetyAgent,
            "ContextAgent": ContextAgent,
            "ResponseAgent": ResponseAgent,
        }
        return agents[name](services or self.services)


def state_to_context(state: AgentState) -> AgentNodeContext:
    artifacts = []
    for key in (
        "memory",
        "intent",
        "risk",
        "context",
        "response_candidate",
        "output_safety",
    ):
        value = state.get(key)
        if value:
            artifacts.append(artifact_from_state(value))
    return AgentNodeContext(
        turn_id=str(state.get("turn_id", "")),
        user_id=state.get("user_id"),
        session_id=str(state.get("session_id", "")),
        model_input=str(state.get("model_input", "")),
        artifacts=tuple(artifacts),
        domain_events=tuple(event_from_state(item) for item in state.get("domain_events", [])),
    )


async def prefetch_memory(state: AgentState, runtime: Runtime[GraphRuntimeContext]) -> dict[str, Any]:
    return await _run_agent(
        state,
        runtime,
        "ContextAgent",
        "prefetch_memory",
    )


async def understand_intent(state: AgentState, runtime: Runtime[GraphRuntimeContext]) -> dict[str, Any]:
    return await _run_agent(
        state,
        runtime,
        "UnderstandingAgent",
        "understand_intent",
    )


async def assess_safety(state: AgentState, runtime: Runtime[GraphRuntimeContext]) -> dict[str, Any]:
    return await _run_agent(
        state,
        runtime,
        "SafetyAgent",
        "assess_safety",
    )


def route_context_node(state: AgentState) -> dict[str, Any]:
    return {"steps": [_step(state, "LangGraph", "route_context", "已合并意图与风险并行分支")]}


async def gather_context(state: AgentState, runtime: Runtime[GraphRuntimeContext]) -> dict[str, Any]:
    return await _run_agent(
        state,
        runtime,
        "ContextAgent",
        "gather_context",
    )


def skip_context(state: AgentState) -> dict[str, Any]:
    return {"steps": [_step(state, "LangGraph", "skip_context", "CHAT/LOW 跳过完整 RAG")]}


async def generate_response(state: AgentState, runtime: Runtime[GraphRuntimeContext]) -> dict[str, Any]:
    revision_count = int(state.get("revision_count", 0))
    current = state.get("response_candidate") or {}
    return await _run_agent(
        state,
        runtime,
        "ResponseAgent",
        "generate_response",
        revision_count=revision_count,
        revision_of=str(current.get("id", "")) if revision_count else "",
    )


async def review_response(state: AgentState, runtime: Runtime[GraphRuntimeContext]) -> dict[str, Any]:
    update = await _run_agent(
        state,
        runtime,
        "SafetyAgent",
        "review_response",
    )
    review = update.get("output_safety") or {}
    status = str((review.get("payload") or {}).get("status", ""))
    if status == OutputSafetyStatus.REVISE.value:
        update["revision_count"] = int(state.get("revision_count", 0)) + 1
    return update


def compact_context(state: AgentState) -> dict[str, Any]:
    update: dict[str, Any] = {
        "reactive_compaction_used": True,
        "generation_route": "GENERATE",
        "domain_events": [
            {
                "type": AgentEventType.CONTEXT_COMPACTED.value,
                "actor": "LangGraph",
                "message": "PROMPT_TOO_LONG 后执行一次 Reactive Context 规划",
                "metadata": {"reactive": True},
            }
        ],
        "steps": [_step(state, "LangGraph", "compact_context", "启用一次 Reactive Context 规划")],
    }
    revised = []
    for key in ("memory", "context"):
        current = state.get(key)
        if not current:
            continue
        artifact = copy.deepcopy(current)
        artifact["id"] = f"{artifact.get('owner', 'LangGraph')}:{key}:reactive:{uuid.uuid4().hex[:10]}"
        metadata = dict(artifact.get("metadata") or {})
        metadata["reactiveCompacted"] = True
        metadata["contextRecoveryAttempt"] = 1
        artifact["metadata"] = metadata
        update[key] = artifact
        revised.append(artifact)
    if revised:
        update["artifacts"] = [artifact_checkpoint_summary(item) for item in revised]
    return update


def safe_fallback_node(state: AgentState) -> dict[str, Any]:
    risk = _risk_level(state)
    response = _new_artifact(
        "response_candidate",
        "LangGraphFallback",
        {
            "text": safe_fallback(risk),
            "model": "deterministic-fallback",
            "provider": "local",
            "latencyMs": 0.0,
            "mode": "failure_fallback",
            "intent": _intent_type(state).value,
            "risk": risk.value,
            "revisionCount": int(state.get("revision_count", 0)),
            "generationStatus": "fallback",
            "responseAgent": "LangGraphFallback",
        },
        confidence=1.0,
    )
    review = _new_artifact(
        "output_safety",
        "SafetyAgent",
        {
            "status": OutputSafetyStatus.FALLBACK.value,
            "text": safe_fallback(risk),
            "reason": "确定性 fail-closed fallback",
            "responseArtifactId": response["id"],
            "risk": risk.value,
        },
        confidence=1.0,
        metadata={"responseArtifactId": response["id"]},
    )
    return {
        "response_candidate": response,
        "output_safety": review,
        "artifacts": [
            artifact_checkpoint_summary(response),
            artifact_checkpoint_summary(review),
        ],
        "generation_route": "FALLBACK",
        "domain_events": [
            {
                "type": AgentEventType.FALLBACK_APPLIED.value,
                "actor": "LangGraphFallback",
                "artifact_id": response["id"],
                "message": "已应用确定性安全回复",
                "metadata": {"risk": risk.value},
            }
        ],
        "steps": [_step(state, "LangGraphFallback", "safe_fallback", risk.value)],
    }


def finalize(state: AgentState) -> dict[str, Any]:
    response = state.get("response_candidate") or {}
    review = state.get("output_safety") or {}
    review_payload = review.get("payload") or {}
    matched = (review.get("metadata") or {}).get("responseArtifactId") == response.get("id")
    accepted = review_payload.get("status") in {
        OutputSafetyStatus.APPROVED.value,
        OutputSafetyStatus.FALLBACK.value,
    }
    if not response or not matched or not accepted:
        fallback = safe_fallback_node(state)
        response = fallback["response_candidate"]
        review = fallback["output_safety"]
        review_payload = review["payload"]
        fallback.update(
            {
                "status": "COMPLETED",
                "final_response": str(review_payload["text"]),
                "final_artifact_id": str(response["id"]),
            }
        )
        return fallback
    return {
        "status": "COMPLETED",
        "final_response": str(review_payload.get("text", "")) or safe_fallback(_risk_level(state)),
        "final_artifact_id": str(response["id"]),
        "domain_events": [
            {
                "type": AgentEventType.FINAL_ACCEPTED.value,
                "actor": "LangGraph",
                "artifact_id": str(response["id"]),
                "message": "候选回复与安全审核 ID 匹配，已完成验收",
                "metadata": {"reviewArtifactId": review.get("id", "")},
            }
        ],
        "steps": [_step(state, "LangGraph", "finalize", str(response["id"]))],
    }


async def _run_agent(
    state: AgentState,
    runtime: Runtime[GraphRuntimeContext],
    agent_name: str,
    node_name: str,
    *,
    revision_count: int = 0,
    revision_of: str = "",
) -> dict[str, Any]:
    context = runtime.context
    started = time.perf_counter()
    services = context.services
    if agent_name == "ResponseAgent":
        async def graph_token_sink(token: str) -> None:
            runtime.stream_writer({"kind": "token", "text": token})

        services = replace(services, response_token_sink=graph_token_sink)
    try:
        async with asyncio.timeout(max(0.1, context.node_timeout_seconds)):
            agent = context.agent(agent_name, services=services)
            node_context = state_to_context(state)
            if node_name == "prefetch_memory":
                result = await agent.prefetch(node_context)
            elif node_name == "understand_intent":
                result = await agent.run(node_context)
            elif node_name == "assess_safety":
                result = await agent.assess(node_context)
            elif node_name == "gather_context":
                result = await agent.gather(node_context)
            elif node_name == "generate_response":
                result = await agent.run(
                    node_context,
                    revision_count=revision_count,
                    revision_of=revision_of,
                )
            elif node_name == "review_response":
                response = node_context.latest_artifact("response_candidate")
                if response is None:
                    raise ValueError("缺少待审核 response_candidate")
                result = agent.review(node_context, response)
            else:
                raise ValueError(f"未知 Agent 节点：{node_name}")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        failure = classify_agent_error(exc)
        attempt = getattr(runtime.execution_info, "node_attempt", 1)
        if failure.retryable and failure.kind != AgentFailureKind.CONTEXT_OVERFLOW and attempt < context.node_max_attempts:
            get_runtime_metrics().record_graph_node(
                node_name,
                "retry",
                (time.perf_counter() - started) * 1000,
            )
            raise
        if node_name == "generate_response" and failure.kind == AgentFailureKind.CONTEXT_OVERFLOW:
            unrecoverable = bool(state.get("reactive_compaction_used", False))
            update: dict[str, Any] = {
                "generation_route": "FALLBACK" if unrecoverable else "COMPACT",
                "errors": [_error(node_name, failure, attempt)],
                "steps": [_step(state, agent_name, node_name, failure.kind.value)],
            }
            if unrecoverable:
                update["domain_events"] = [
                    {
                        "type": AgentEventType.CONTEXT_UNRECOVERABLE.value,
                        "actor": agent_name,
                        "message": failure.message[:500],
                        "metadata": {"attempt": attempt},
                    }
                ]
            get_runtime_metrics().record_graph_node(
                node_name,
                "fallback" if unrecoverable else "compact",
                (time.perf_counter() - started) * 1000,
            )
            return update
        fallback = _fallback_result(state, agent_name, node_name, failure.message)
        update = _result_update(state, fallback, node_name)
        update["errors"] = [_error(node_name, failure, attempt)]
        get_runtime_metrics().record_graph_node(
            node_name,
            "fallback",
            (time.perf_counter() - started) * 1000,
        )
        return update
    update = _result_update(state, result, node_name)
    if node_name == "generate_response":
        update["generation_route"] = "REVIEW"
    get_runtime_metrics().record_graph_node(
        node_name,
        "success",
        (time.perf_counter() - started) * 1000,
    )
    return update


def _result_update(state: AgentState, result: AgentNodeResult, node_name: str) -> dict[str, Any]:
    artifacts = [artifact_to_state(item) for item in result.artifacts]
    update: dict[str, Any] = {
        "artifacts": [artifact_checkpoint_summary(item) for item in artifacts],
        "steps": [_step(state, _actor(result, node_name), node_name, "完成")],
    }
    for artifact in artifacts:
        if artifact["kind"] in {
            "memory",
            "intent",
            "risk",
            "context",
            "response_candidate",
            "output_safety",
        }:
            update[artifact["kind"]] = artifact
    events = [event_to_state(event) for event in result.events if event.type in DOMAIN_EVENT_TYPES]
    if events:
        update["domain_events"] = events
    return update


def _fallback_result(
    state: AgentState,
    agent_name: str,
    node_name: str,
    reason: str,
) -> AgentNodeResult:
    risk = _risk_level(state)
    if node_name == "prefetch_memory":
        artifact = _typed_artifact(
            "memory",
            agent_name,
            {"history": [], "memoryBrief": "无相关历史记忆。", "longTermMemoryIndex": [], "longTermMemories": [], "longTermMemoryContext": "无相关长期记忆。"},
            node_name,
        )
    elif node_name == "understand_intent":
        text = str(state.get("model_input", ""))
        intent = IntentType.CONSULT if detect_risk_signal(text).level != RiskLevel.LOW or has_consult_signal(text) else IntentType.CHAT
        artifact = _typed_artifact("intent", agent_name, {"intent": intent.value, "topic": "fallback", "reason": reason[:500]}, node_name)
    elif node_name == "assess_safety":
        assessment = _fallback_assessment(str(state.get("model_input", "")))
        artifact = _typed_artifact(
            "risk",
            agent_name,
            {
                "risk": assessment.risk.value,
                "emotion": assessment.emotion.value,
                "emotionScore": assessment.emotion_score,
                "confidence": assessment.confidence,
                "summary": assessment.summary,
                "assessment": assessment,
            },
            node_name,
        )
    elif node_name == "gather_context":
        memory = (state.get("memory") or {}).get("payload") or {}
        artifact = _typed_artifact(
            "context",
            agent_name,
            {
                "memoryBrief": memory.get("memoryBrief", "无相关历史记忆。"),
                "modelHistory": memory.get("history", []),
                "longTermMemoryContext": "无相关长期记忆。",
                "retrievedKnowledge": [],
                "skillContext": "",
                "selectedSkills": [],
                "skillSelectionStrategy": "deterministic_failure_fallback",
            },
            node_name,
        )
    elif node_name == "review_response":
        response = state.get("response_candidate") or {}
        artifact = _typed_artifact(
            "output_safety",
            agent_name,
            {
                "status": OutputSafetyStatus.FALLBACK.value,
                "text": safe_fallback(risk),
                "reason": "安全审核异常，已 fail-closed",
                "responseArtifactId": response.get("id", ""),
                "risk": risk.value,
            },
            node_name,
            metadata={"responseArtifactId": response.get("id", "")},
        )
    else:
        artifact = _typed_artifact(
            "response_candidate",
            agent_name,
            {
                "text": safe_fallback(risk),
                "model": "deterministic-fallback",
                "provider": "local",
                "latencyMs": 0.0,
                "mode": "failure_fallback",
                "intent": _intent_type(state).value,
                "risk": risk.value,
                "revisionCount": int(state.get("revision_count", 0)),
                "generationStatus": "fallback",
                "responseAgent": agent_name,
            },
            node_name,
        )
    return AgentNodeResult(artifacts=(artifact,))


def _fallback_assessment(text: str) -> PsychologyAssessment:
    rule = detect_risk_signal(text)
    if rule.level == RiskLevel.HIGH:
        return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.97, rule.reason)
    result = heuristic(text)
    if rule.level == RiskLevel.MEDIUM and result.risk == RiskLevel.LOW:
        return PsychologyAssessment(result.emotion, max(result.emotion_score, 3.0), RiskLevel.MEDIUM, 0.82, rule.reason)
    return result


def _typed_artifact(
    kind: str,
    owner: str,
    payload: dict[str, Any],
    node_name: str,
    *,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> AgentArtifact:
    return AgentArtifact(
        id=f"{owner}:{kind}:fallback:{uuid.uuid4().hex[:10]}",
        owner=owner,
        kind=kind,
        payload=payload,
        confidence=confidence,
        node_name=node_name,
        metadata=metadata or {"fallback": True},
    )


def _new_artifact(
    kind: str,
    owner: str,
    payload: dict[str, Any],
    *,
    confidence: float,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return artifact_to_state(
        AgentArtifact(
            id=f"{owner}:{kind}:{uuid.uuid4().hex[:10]}",
            owner=owner,
            kind=kind,
            payload=payload,
            confidence=confidence,
            node_name=kind,
            metadata=metadata or {},
        )
    )


def _intent_type(state: AgentState) -> IntentType:
    payload = (state.get("intent") or {}).get("payload") or {}
    try:
        intent = IntentType(str(payload.get("intent", IntentType.CHAT.value)).upper())
        return IntentType.CONSULT if intent == IntentType.RISK else intent
    except ValueError:
        return IntentType.CHAT


def _risk_level(state: AgentState) -> RiskLevel:
    payload = (state.get("risk") or {}).get("payload") or {}
    try:
        return RiskLevel(str(payload.get("risk", RiskLevel.LOW.value)).upper())
    except ValueError:
        return RiskLevel.LOW


def _actor(result: AgentNodeResult, default: str) -> str:
    if result.artifacts:
        return result.artifacts[-1].owner
    return default


def _step(state: AgentState, agent: str, action: str, observation: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "action": action,
        "observation": observation[:500],
    }


def _error(node_name: str, failure, attempt: int) -> dict[str, Any]:
    return {
        "node": node_name,
        "kind": failure.kind.value,
        "error_type": failure.error_type,
        "message": failure.message[:500],
        "attempt": attempt,
        "retryable": failure.retryable,
    }
