"""生产可靠性离线评测入口：python -m app.context_eval.runner --json。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from app.context.compaction import CompactionEngine
from app.context.contracts import ContextEnvelope, ContextPlan, ContextSection
from app.context.planner import ContextPlanner
from app.context.tokens import ConservativeEstimator
from app.context_eval.faults import (
    FaultInjectingProvider,
    load_optional_cache,
    parse_structured_or_default,
)
from app.core.enums import RiskLevel
from app.llm.capabilities import ModelCapabilities
from app.llm.contracts import ModelRequest
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.gateway import ModelGateway
from app.llm.recovery import RecoveryEvent, RecoveryPolicy
from app.schemas.dtos import AiMessage


DATASET_PATH = Path(__file__).with_name("datasets") / "context-cases-v1.json"
REQUIRED_SECTION_IDS = frozenset(
    {"global.identity", "global.safety", "agent.contract", "user.current"}
)


@dataclass(frozen=True)
class EvalCase:
    id: str
    fault: str
    risk_level: str
    expected_outcome: str


@dataclass(frozen=True)
class EvalDataset:
    version: str
    thresholds: dict[str, float | int]
    cases: tuple[EvalCase, ...]


@dataclass(frozen=True)
class EvalCaseResult:
    id: str
    fault: str
    risk_level: str
    expected_outcome: str
    observed_outcome: str
    passed: bool
    required_sections_preserved: bool
    tokens_before: int
    tokens_after: int
    token_reduction_ratio: float
    retry_count: int
    reactive_compactions: int
    reactive: bool
    local_calls: int
    cloud_calls: int
    latency_ms: float
    terminal_error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return {_camel(key): value for key, value in raw.items()}


@dataclass(frozen=True)
class EvalReport:
    dataset_version: str
    passed: bool
    cases: tuple[EvalCaseResult, ...]
    aggregate: dict[str, float | int]

    @property
    def failed_cases(self) -> int:
        return sum(not case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "datasetVersion": self.dataset_version,
            "passed": self.passed,
            "failedCases": self.failed_cases,
            "aggregate": self.aggregate,
            "cases": [case.to_dict() for case in self.cases],
        }


def load_dataset(path: Path | None = None) -> EvalDataset:
    payload = json.loads((path or DATASET_PATH).read_text(encoding="utf-8"))
    return EvalDataset(
        version=str(payload["version"]),
        thresholds=dict(payload["thresholds"]),
        cases=tuple(
            EvalCase(
                id=str(item["id"]),
                fault=str(item["fault"]),
                risk_level=str(item["riskLevel"]),
                expected_outcome=str(item["expectedOutcome"]),
            )
            for item in payload["cases"]
        ),
    )


async def evaluate_dataset(
    *,
    case_ids: set[str] | None = None,
    path: Path | None = None,
) -> EvalReport:
    dataset = load_dataset(path)
    selected = tuple(
        case for case in dataset.cases if case_ids is None or case.id in case_ids
    )
    if not selected:
        raise ValueError("没有匹配的评测用例")
    results = tuple([await _evaluate_case(case, dataset.thresholds) for case in selected])
    before = sum(case.tokens_before for case in results)
    after = sum(case.tokens_after for case in results)
    aggregate = {
        "caseCount": len(results),
        "passedCases": sum(case.passed for case in results),
        "tokenReductionRatio": round((before - after) / before, 4) if before else 0.0,
        "cloudCalls": sum(case.cloud_calls for case in results),
        "retryCount": sum(case.retry_count for case in results),
        "latencyMs": round(sum(case.latency_ms for case in results), 3),
    }
    return EvalReport(
        dataset_version=dataset.version,
        passed=all(case.passed for case in results),
        cases=results,
        aggregate=aggregate,
    )


async def _evaluate_case(
    case: EvalCase,
    thresholds: dict[str, float | int],
) -> EvalCaseResult:
    started = time.perf_counter()
    risk = RiskLevel(case.risk_level)
    envelope = _envelope(case, risk)
    capabilities = _capabilities()
    planner = ContextPlanner(
        ConservativeEstimator(),
        reserve_tokens=128,
        margin_ratio=0,
        category_budget_ratios={
            "global": 1.0,
            "agent": 1.0,
            "user": 1.0,
            "conversation": 0.4,
            "rag": 0.3,
            "memory": 0.2,
        },
    )
    plan = planner.plan(envelope, capabilities, capabilities.default_output_tokens)
    local = FaultInjectingProvider("ollama", case.fault)
    cloud = FaultInjectingProvider("openai")
    events: list[RecoveryEvent] = []
    gateway = ModelGateway(
        {"ollama": local, "openai": cloud},
        cloud_provider="openai",
        cloud_model="eval-cloud",
        recovery_policy=RecoveryPolicy(
            max_transient_retries=int(thresholds["maximumTransientRetries"]),
            max_stream_retries=1,
            deadline_seconds=5,
            base_delay_seconds=0,
            jitter_ratio=0,
        ),
        on_event=events.append,
    )
    observed = "success"
    terminal_error = ""
    reactive_compactions = 0

    request = _request(case, risk, plan)
    try:
        if case.fault == "prompt_too_long":
            try:
                await gateway.complete(request)
            except ModelError as error:
                if error.code is not ModelErrorCode.PROMPT_TOO_LONG:
                    raise
                plan = CompactionEngine(planner).reactive_plan(envelope, capabilities)
                reactive_compactions = 1
                request = _request(case, risk, plan)
                await gateway.complete(request)
                observed = "recovered"
        elif case.fault == "invalid_json":
            result = await gateway.complete(request)
            parsed = parse_structured_or_default(
                result.text,
                default={"status": "degraded", "items": []},
            )
            observed = "deterministic_fallback" if parsed.degraded else "success"
        elif case.fault == "redis_unavailable":
            _, degraded = load_optional_cache(
                lambda: (_ for _ in ()).throw(ConnectionError("injected redis outage")),
                default=(),
            )
            observed = "degraded" if degraded else "success"
        elif case.fault == "stream_interrupted":
            _ = [event async for event in gateway.stream(replace(request, stream=True))]
            observed = "recovered"
        else:
            result = await gateway.complete(request)
            observed = "cloud_fallback" if result.provider == "openai" else "success"
    except ModelError as error:
        terminal_error = error.code.value
        observed = "contained" if risk is RiskLevel.HIGH and cloud.calls == 0 else "failed"

    selected_ids = {section.id for section in plan.sections}
    required_preserved = REQUIRED_SECTION_IDS <= selected_ids
    reduction = (
        (plan.tokens_before - plan.tokens_after) / plan.tokens_before
        if plan.tokens_before
        else 0.0
    )
    retry_count = sum(
        event.kind in {"retry_scheduled", "stream_retry_scheduled"} for event in events
    )
    passed = all(
        (
            observed == case.expected_outcome,
            required_preserved,
            reduction >= float(thresholds["minimumTokenReductionRatio"]),
            retry_count <= int(thresholds["maximumTransientRetries"]),
            reactive_compactions <= int(thresholds["maximumReactiveCompactions"]),
            not (
                risk is RiskLevel.HIGH
                and cloud.calls > int(thresholds["maximumCloudCallsForHighRisk"])
            ),
        )
    )
    return EvalCaseResult(
        id=case.id,
        fault=case.fault,
        risk_level=case.risk_level,
        expected_outcome=case.expected_outcome,
        observed_outcome=observed,
        passed=passed,
        required_sections_preserved=required_preserved,
        tokens_before=plan.tokens_before,
        tokens_after=plan.tokens_after,
        token_reduction_ratio=round(reduction, 4),
        retry_count=retry_count,
        reactive_compactions=reactive_compactions,
        reactive=plan.reactive,
        local_calls=local.calls,
        cloud_calls=cloud.calls,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        terminal_error_code=terminal_error,
    )


def _envelope(case: EvalCase, risk: RiskLevel) -> ContextEnvelope:
    required = (
        ContextSection("global.identity", "global", "identity rules " * 20, 100, True),
        ContextSection("global.safety", "global", "safety boundary " * 20, 100, True),
        ContextSection("agent.contract", "agent", "agent contract " * 20, 100, True),
        ContextSection("user.current", "user", "current request " * 20, 100, True),
    )
    optional = tuple(
        ContextSection(
            id=f"conversation.history.{index}",
            category="conversation",
            content=("EVAL_SECRET_SENTINEL historical detail " * 90),
            priority=40 - index,
            provenance_ids=(f"message:{index}",),
        )
        for index in range(6)
    ) + tuple(
        ContextSection(
            id=f"rag.extra.{index}",
            category="rag",
            content=("retrieved evidence " * 80),
            priority=30 - index,
            provenance_ids=(f"document:{index}",),
        )
        for index in range(4)
    )
    return ContextEnvelope(
        request_id=f"eval-{case.id}",
        agent_name="ResponseAgent",
        task_name="context_recovery_eval",
        sections=required + optional,
        session_id="eval-session",
        risk_level=risk,
    )


def _capabilities() -> ModelCapabilities:
    return ModelCapabilities(
        provider="ollama",
        model="eval-local",
        context_window=2048,
        default_output_tokens=256,
        maximum_output_tokens=512,
        cloud=False,
    )


def _request(case: EvalCase, risk: RiskLevel, plan: ContextPlan) -> ModelRequest:
    content = "\n".join(section.content for section in plan.sections)
    return ModelRequest(
        request_id=f"eval-{case.id}",
        agent_name="ResponseAgent",
        task_name="context_recovery_eval",
        risk_level=risk,
        messages=(AiMessage(role="user", content=content),),
        preferred_provider="ollama",
        preferred_model="eval-local",
        max_output_tokens=256,
        cloud_egress_allowed=risk is not RiskLevel.HIGH,
        sanitized=True,
        context_section_ids=tuple(section.id for section in plan.sections),
        context_plan_hash=plan.plan_hash,
    )


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.title() for part in rest)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行上下文压缩与错误恢复离线评测")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--case", action="append", dest="case_ids", help="只运行指定用例")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    report = asyncio.run(
        evaluate_dataset(case_ids=set(args.case_ids) if args.case_ids else None)
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{report.dataset_version}: "
            f"{report.aggregate['passedCases']}/{report.aggregate['caseCount']} 通过，"
            f"token 压缩率 {report.aggregate['tokenReductionRatio']:.1%}"
        )
        for case in report.cases:
            status = "PASS" if case.passed else "FAIL"
            print(f"[{status}] {case.id}: {case.observed_outcome}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
