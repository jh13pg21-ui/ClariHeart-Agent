from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace

from app.agents.autonomous import CoordinatorAgent
from app.agents.events import (
    AgentEvent,
    AgentEventType,
    AgentTask,
    CollaborationBlackboard,
    PRIORITY_ORDER,
    TaskPriority,
    TaskStatus,
)
from app.agents.recovery import (
    AgentFailureKind,
    classify_agent_error,
    compact_board_for_retry,
    has_reactive_context_retry,
    retry_delay,
)
from app.agents.registry import AgentCapability, AgentRegistry
from app.core.config import Settings
from app.core.enums import EmotionLabel, IntentType, RiskLevel
from app.services.ai import has_consult_signal
from app.services.assessment import PsychologyAssessment
from app.services.output_safety import OutputSafetyStatus, safe_fallback
from app.services.risk_rules import detect_risk_signal
from app.services.skills import MindBridgeSkillLibrary


@dataclass(frozen=True)
class AgentInvocationOutcome:
    task: AgentTask
    result: object


class EventDrivenCoordinator:
    """Claim-based coordinator.

    This class owns budgets and acceptance policy. It does not encode an agent
    chain; all worker execution comes from agents claiming open tasks.
    """

    def __init__(self, registry: AgentRegistry, coordinator_agent: CoordinatorAgent, settings: Settings):
        self.registry = registry
        self.coordinator_agent = coordinator_agent
        self.settings = settings
        self.max_rounds = int(getattr(settings, "agent_max_rounds", 8))
        self.max_claims_per_round = int(getattr(settings, "agent_max_claims_per_round", 4))
        self.max_claims_per_agent = int(getattr(settings, "agent_max_claims_per_agent", 3))
        self.final_min_confidence = float(getattr(settings, "agent_final_acceptance_min_confidence", 0.6))
        self.task_timeout_seconds = float(getattr(settings, "agent_task_timeout_seconds", 70.0))
        self.task_max_attempts = max(1, int(getattr(settings, "agent_task_max_attempts", 2)))
        self.retry_base_seconds = float(getattr(settings, "agent_retry_base_seconds", 0.2))
        self.retry_max_seconds = float(getattr(settings, "agent_retry_max_seconds", 2.0))
        self.retry_jitter_ratio = float(getattr(settings, "agent_retry_jitter_ratio", 0.25))

    async def run(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        board = self._ensure_root_task(board)
        claim_counts: dict[str, int] = defaultdict(int)
        for round_number in range(1, self.max_rounds + 1):
            board = board.append_event(
                AgentEvent(
                    type=AgentEventType.ROUND_STARTED,
                    actor=self.coordinator_agent.name,
                    message=f"round={round_number}",
                    metadata={"round": round_number},
                )
            )
            board = self._derive_missing_work(board)
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
            candidates = self._claim_candidates(board, claim_counts)
            if not candidates:
                board = self._derive_missing_work(board, force_response=True)
                candidates = self._claim_candidates(board, claim_counts)
                if not candidates:
                    break
            invocations = []
            for task, candidate in candidates:
                current_task = board.tasks.get(task.id, task)
                claimed_task = current_task.claim(candidate.agent.profile.name)
                board = board.update_task(claimed_task).append_event(
                    AgentEvent(
                        type=AgentEventType.TASK_CLAIMED,
                        actor=candidate.agent.profile.name,
                        task_id=task.id,
                        message=candidate.decision.reason,
                        metadata={"confidence": candidate.decision.confidence},
                    )
                )
                invocations.append((claimed_task, candidate))
                claim_counts[candidate.agent.profile.name] += 1
            round_board = board
            results = [None] * len(invocations)
            async with asyncio.TaskGroup() as group:
                for index, (task, candidate) in enumerate(invocations):
                    group.create_task(
                        self._run_candidate(index, task, candidate, round_board, results)
                    )
            completed = [
                (results[index].task, candidate, results[index].result)
                for index, (task, candidate) in enumerate(invocations)
            ]
            completed.sort(key=self._merge_key)
            for task, candidate, result in completed:
                board = board.apply_turn_result(task, candidate.agent.profile.name, result)
            board = self._derive_missing_work(board)
            board = self._try_accept_final(board)
            if board.final_artifact_id:
                return board
        return board.append_event(
            AgentEvent(
                type=AgentEventType.BUDGET_EXHAUSTED,
                actor=self.coordinator_agent.name,
                message="event-driven agent budget exhausted before final acceptance",
            )
        )

    async def _run_candidate(self, index, task, candidate, round_board, results) -> None:
        retry_events = []
        attempt_board = round_board
        max_attempts = max(1, min(task.max_attempts, self.task_max_attempts))
        effective_task = task
        for attempt in range(1, max_attempts + 1):
            effective_task = replace(task, attempts=task.attempts + attempt - 1)
            try:
                async with asyncio.timeout(self.task_timeout_seconds):
                    result = await candidate.agent.act(effective_task, attempt_board)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                failure = classify_agent_error(exc)
                error = f"{failure.error_type}: {failure.message}"[:1000]
                retryable = failure.retryable and not (
                    failure.kind == AgentFailureKind.CONTEXT_OVERFLOW
                    and has_reactive_context_retry(attempt_board)
                )
                if retryable and attempt < max_attempts:
                    retry_events.append(
                        AgentEvent(
                            type=AgentEventType.TASK_RETRY_SCHEDULED,
                            actor=candidate.agent.profile.name,
                            task_id=task.id,
                            message=error,
                            metadata={
                                "attempt": attempt,
                                "maxAttempts": max_attempts,
                                "failureKind": failure.kind.value,
                            },
                        )
                    )
                    if failure.kind == AgentFailureKind.CONTEXT_OVERFLOW:
                        attempt_board = compact_board_for_retry(attempt_board)
                    delay = retry_delay(
                        attempt,
                        self.retry_base_seconds,
                        self.retry_max_seconds,
                        self.retry_jitter_ratio,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                result = self._fallback_result(effective_task, candidate.agent.profile.name, round_board, failure)
                result = replace(
                    result,
                    events=(*retry_events, *result.events),
                    task_status=TaskStatus.FAILED,
                    task_error=error,
                )
                results[index] = AgentInvocationOutcome(effective_task, result)
                return
            result = replace(result, events=(*retry_events, *result.events))
            results[index] = AgentInvocationOutcome(effective_task, result)
            return

    @staticmethod
    def _merge_key(item) -> tuple[int, str, str]:
        task, candidate, result = item
        artifact_kinds = {artifact.kind for artifact in result.artifacts}
        has_safety_override = any(
            event.type == AgentEventType.SAFETY_OVERRIDE for event in result.events
        )
        if has_safety_override or "safety_event" in artifact_kinds:
            category = 0
        elif "risk" in artifact_kinds:
            category = 1
        elif "intent" in artifact_kinds:
            category = 2
        elif artifact_kinds.intersection({"memory", "context"}):
            category = 3
        else:
            category = 4
        return category, task.id, candidate.agent.profile.name

    def _ensure_root_task(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if board.tasks:
            return board
        root = self.coordinator_agent.root_task(board)
        return board.add_task(root).append_event(
            AgentEvent(type=AgentEventType.TASK_CREATED, actor=self.coordinator_agent.name, task_id=root.id, message=root.title)
        )

    def _derive_missing_work(self, board: CollaborationBlackboard, force_response: bool = False) -> CollaborationBlackboard:
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="memory",
            task_id="task:prefetch-memory",
            title="Prefetch conversation memory",
            capability=AgentCapability.CONTEXT,
            priority=TaskPriority.HIGH,
            condition=board.user_input != "",
        )
        memory_ready = board.latest_artifact("memory") is not None
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="intent",
            task_id="task:understand",
            title="Understand user turn",
            capability=AgentCapability.UNDERSTANDING,
            priority=TaskPriority.HIGH,
            condition=board.user_input != "" and memory_ready,
        )
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="risk",
            task_id="task:assess-safety",
            title="Assess safety risk",
            capability=AgentCapability.SAFETY,
            priority=TaskPriority.CRITICAL if _hard_high_risk(board.user_input) else TaskPriority.HIGH,
            condition=board.user_input != "" and memory_ready,
        )
        intent = _intent_value(board)
        risk = _risk_value(board)
        needs_context = intent == IntentType.CONSULT or risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="context",
            task_id="task:gather-context",
            title="Gather contextual evidence",
            capability=AgentCapability.CONTEXT,
            priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.NORMAL,
            condition=(
                needs_context
                and board.latest_artifact("intent") is not None
                and board.latest_artifact("risk") is not None
                and board.latest_artifact("memory") is not None
            ),
        )
        has_response = board.latest_artifact("response_candidate") is not None
        can_request_response = force_response or (
            board.latest_artifact("intent") is not None
            and board.latest_artifact("risk") is not None
            and (not needs_context or board.latest_artifact("context") is not None)
        )
        board = self._ensure_task_for_missing_artifact(
            board,
            artifact_kind="response_candidate",
            task_id="task:propose-response",
            title="Propose candidate response",
            capability=AgentCapability.RESPONSE,
            priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.HIGH,
            condition=can_request_response and not has_response,
        )
        response = board.latest_artifact("response_candidate")
        review = board.latest_artifact("output_safety")
        if response and (review is None or review.metadata.get("responseArtifactId") != response.id):
            board = self._ensure_task(
                board,
                AgentTask(
                    id=f"task:review-response:{response.id}",
                    title="Review candidate response safety",
                    description="Safety review is required before final acceptance.",
                    priority=TaskPriority.CRITICAL if risk == RiskLevel.HIGH else TaskPriority.HIGH,
                    required_capabilities=frozenset({AgentCapability.SAFETY.value}),
                    created_by=self.coordinator_agent.name,
                    metadata={"kind": "safety_review", "responseArtifactId": response.id},
                ),
            )
        return board

    def _fallback_result(self, task, agent_name, board, failure):
        from app.agents.events import AgentArtifact, AgentTurnResult

        kind = str(task.metadata.get("kind", ""))
        risk = _risk_value(board)
        intent = _intent_value(board)
        artifact_kind = "agent_failure"
        payload = {
            "failureKind": failure.kind.value,
            "errorType": failure.error_type,
            "message": failure.message[:500],
            "fallback": True,
        }
        confidence = 1.0
        metadata = {"recoveredFromAgentFailure": True}

        if kind == "memory":
            artifact_kind = "memory"
            payload |= {
                "history": [],
                "memoryBrief": "记忆服务暂时不可用，本轮不注入历史。",
                "longTermMemoryIndex": [],
                "longTermMemories": [],
                "longTermMemoryContext": "无相关长期记忆。",
            }
        elif kind == "intent":
            artifact_kind = "intent"
            rule_risk = detect_risk_signal(board.user_input).level
            fallback_intent = IntentType.CONSULT if (
                rule_risk in {RiskLevel.MEDIUM, RiskLevel.HIGH}
                or has_consult_signal(board.user_input)
            ) else IntentType.CHAT
            payload |= {
                "intent": fallback_intent.value,
                "topic": "safety" if _hard_high_risk(board.user_input) else "fallback",
                "reason": "deterministic fallback after UnderstandingAgent failure",
            }
        elif kind == "risk":
            artifact_kind = "risk"
            rule_risk = detect_risk_signal(board.user_input).level
            if rule_risk == RiskLevel.HIGH:
                fallback_risk = RiskLevel.HIGH
                emotion = EmotionLabel.HIGH_RISK
                score = 4.0
            elif rule_risk == RiskLevel.MEDIUM or has_consult_signal(board.user_input):
                fallback_risk = RiskLevel.MEDIUM
                emotion = EmotionLabel.ANXIETY
                score = 3.0
            else:
                fallback_risk = RiskLevel.LOW
                emotion = EmotionLabel.NORMAL
                score = 0.0
            assessment = PsychologyAssessment(
                emotion,
                score,
                fallback_risk,
                0.5,
                "风险评估服务异常，已采用保守规则兜底",
            )
            payload |= {
                "risk": fallback_risk.value,
                "emotion": emotion.value,
                "emotionScore": score,
                "confidence": 0.5,
                "summary": assessment.summary,
                "assessment": assessment,
            }
            if fallback_risk == RiskLevel.HIGH:
                metadata["safetyOverride"] = True
        elif kind == "context":
            artifact_kind = "context"
            memory = board.latest_artifact("memory")
            memory_payload = memory.payload if memory else {}
            try:
                skill_context = MindBridgeSkillLibrary.response_skill_context(
                    intent,
                    risk,
                    board.user_input,
                )
                selected_skills = MindBridgeSkillLibrary.response_skill_names(intent, risk, board.user_input)
            except Exception:
                skill_context = ""
                selected_skills = []
            payload |= {
                "memoryBrief": memory_payload.get("memoryBrief", "无相关历史记忆。"),
                "modelHistory": memory_payload.get("history", []),
                "longTermMemoryContext": "无相关长期记忆。",
                "retrievedKnowledge": [],
                "skillContext": skill_context,
                "selectedSkills": selected_skills,
                "skillSelectionStrategy": "deterministic_failure_fallback",
            }
        elif kind in {"response", "response_candidate"}:
            artifact_kind = "response_candidate"
            payload |= {
                "text": safe_fallback(risk),
                "model": "deterministic-fallback",
                "provider": "local",
                "latencyMs": 0.0,
                "mode": "failure_fallback",
                "intent": intent.value,
                "risk": risk.value,
                "revisionCount": int(task.metadata.get("revisionCount", 0)),
                "generationStatus": "fallback",
                "responseAgent": agent_name,
            }
        elif kind == "safety_review":
            response = board.latest_artifact("response_candidate")
            artifact_kind = "output_safety"
            payload |= {
                "status": OutputSafetyStatus.FALLBACK.value,
                "text": safe_fallback(risk),
                "reason": "SafetyAgent unavailable; fail closed",
                "responseArtifactId": response.id if response else "",
                "risk": risk.value,
            }
            metadata["responseArtifactId"] = response.id if response else ""

        artifact = AgentArtifact(
            id=f"{agent_name}:{artifact_kind}:fallback:{uuid.uuid4().hex[:10]}",
            owner=agent_name,
            kind=artifact_kind,
            payload=payload,
            confidence=confidence,
            task_id=task.id,
            metadata=metadata,
        )
        return AgentTurnResult(
            artifacts=(artifact,),
            events=(
                AgentEvent(
                    type=AgentEventType.TASK_FAILED,
                    actor=agent_name,
                    task_id=task.id,
                    message=f"{failure.error_type}: {failure.message}"[:1000],
                    metadata={"failureKind": failure.kind.value},
                ),
                AgentEvent(
                    type=AgentEventType.TASK_FALLBACK_PUBLISHED,
                    actor=agent_name,
                    task_id=task.id,
                    artifact_id=artifact.id,
                    message=artifact_kind,
                ),
            ),
        )

    def _ensure_task_for_missing_artifact(
        self,
        board: CollaborationBlackboard,
        artifact_kind: str,
        task_id: str,
        title: str,
        capability: AgentCapability,
        priority: TaskPriority,
        condition: bool,
    ) -> CollaborationBlackboard:
        if not condition or board.latest_artifact(artifact_kind) is not None:
            return board
        return self._ensure_task(
            board,
            AgentTask(
                id=task_id,
                title=title,
                description=board.user_input,
                priority=priority,
                required_capabilities=frozenset({capability.value}),
                created_by=self.coordinator_agent.name,
                metadata={"kind": artifact_kind},
            ),
        )

    def _ensure_task(self, board: CollaborationBlackboard, task: AgentTask) -> CollaborationBlackboard:
        if task.id in board.tasks:
            return board
        return board.add_task(task).append_event(
            AgentEvent(type=AgentEventType.TASK_CREATED, actor=self.coordinator_agent.name, task_id=task.id, message=task.title)
        )

    def _claim_candidates(self, board: CollaborationBlackboard, claim_counts: dict[str, int]):
        selected = []
        task_candidates = []
        for task in board.open_tasks():
            for candidate in self.registry.candidate_decisions_for(task, board):
                if claim_counts[candidate.agent.profile.name] >= self.max_claims_per_agent:
                    continue
                task_candidates.append((task, candidate))
        task_candidates.sort(
            key=lambda item: (
                PRIORITY_ORDER[item[0].priority],
                item[1].decision.confidence,
                item[1].agent.profile.name,
            ),
            reverse=True,
        )
        seen = set()
        selected_agents = set()
        for task, candidate in task_candidates:
            key = (task.id, candidate.agent.profile.name)
            if key in seen or candidate.agent.profile.name in selected_agents:
                continue
            selected.append((task, candidate))
            seen.add(key)
            selected_agents.add(candidate.agent.profile.name)
            if len(selected) >= self.max_claims_per_round:
                break
        return selected

    def _try_accept_final(self, board: CollaborationBlackboard) -> CollaborationBlackboard:
        if board.final_artifact_id:
            return board
        response = board.latest_artifact("response_candidate")
        review = board.latest_artifact("output_safety")
        if response is None or review is None:
            return board
        if review.metadata.get("responseArtifactId") != response.id:
            return board
        if review.payload.get("status") not in {"APPROVED", "FALLBACK"}:
            return board
        if response.confidence < self.final_min_confidence:
            return board
        reason = "accepted after autonomous response proposal and SafetyAgent approval"
        self.coordinator_agent.remember_acceptance(response.id, reason)
        return board.accept_final(response.id, self.coordinator_agent.name, reason)


def _intent_value(board: CollaborationBlackboard) -> IntentType:
    artifact = board.latest_artifact("intent")
    if artifact:
        try:
            return IntentType(str(artifact.payload.get("intent", IntentType.CHAT.value)).upper())
        except ValueError:
            return IntentType.CHAT
    if _hard_high_risk(board.user_input):
        return IntentType.CONSULT
    return IntentType.CHAT


def _risk_value(board: CollaborationBlackboard) -> RiskLevel:
    order = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}
    highest = RiskLevel.LOW
    for artifact in board.artifacts_by_kind("risk"):
        try:
            risk = RiskLevel(str(artifact.payload.get("risk", RiskLevel.LOW.value)).upper())
        except ValueError:
            risk = RiskLevel.LOW
        if order[risk] > order[highest]:
            highest = risk
    if any(event.type == AgentEventType.SAFETY_OVERRIDE for event in board.events):
        return RiskLevel.HIGH
    return highest


def _hard_high_risk(text: str) -> bool:
    return detect_risk_signal(text).level == RiskLevel.HIGH
