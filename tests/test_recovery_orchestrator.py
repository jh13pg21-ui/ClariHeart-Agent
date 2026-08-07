import unittest
from dataclasses import dataclass

from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest, ModelResult
from app.llm.errors import ModelError, ModelErrorCode


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="req-recovery",
        agent_name="ResponseAgent",
        task_name="respond",
        risk_level=RiskLevel.LOW,
        messages=(),
        preferred_provider="ollama",
        preferred_model="local-model",
        max_output_tokens=512,
    )


def _result(text: str = "ok") -> ModelResult:
    return ModelResult(
        text=text,
        provider="ollama",
        model="local-model",
        finish_reason="stop",
        input_tokens=10,
        output_tokens=2,
        latency_ms=5,
        request_id="req-recovery",
    )


def _error(
    code: ModelErrorCode,
    *,
    retryable: bool = True,
    retry_after: float | None = None,
) -> ModelError:
    return ModelError(
        code=code,
        message=code.value,
        retryable=retryable,
        provider="ollama",
        model="local-model",
        retry_after_seconds=retry_after,
    )


class SequenceProvider:
    def __init__(self, outcomes: list[ModelResult | ModelError]):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, ModelError):
            raise outcome
        return outcome


@dataclass
class ManualClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


class RecoveryOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_wins_over_exponential_delay(self):
        from app.llm.recovery import RecoveryOrchestrator, RecoveryPolicy

        provider = SequenceProvider(
            [_error(ModelErrorCode.RATE_LIMITED, retry_after=3.0), _result()]
        )
        clock = ManualClock()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await clock.sleep(seconds)

        result = await RecoveryOrchestrator(
            provider,
            policy=RecoveryPolicy(max_transient_retries=2, deadline_seconds=10),
            sleep=sleep,
            clock=clock,
            random_source=lambda: 0.9,
        ).complete(_request())

        self.assertEqual(result.text, "ok")
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(provider.calls, 2)

    async def test_exponential_backoff_uses_independent_transient_budget(self):
        from app.llm.recovery import RecoveryOrchestrator, RecoveryPolicy

        provider = SequenceProvider(
            [
                _error(ModelErrorCode.NETWORK),
                _error(ModelErrorCode.OVERLOADED),
                _result(),
            ]
        )
        clock = ManualClock()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            await clock.sleep(seconds)

        orchestrator = RecoveryOrchestrator(
            provider,
            policy=RecoveryPolicy(
                max_transient_retries=2,
                deadline_seconds=5,
                jitter_ratio=0,
            ),
            sleep=sleep,
            clock=clock,
        )
        result = await orchestrator.complete(_request())

        self.assertEqual(result.text, "ok")
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(orchestrator.last_state.attempt, 3)
        self.assertEqual(orchestrator.last_state.transient_retries, 2)
        self.assertEqual(orchestrator.last_state.consecutive_overloads, 0)

    async def test_deadline_stops_retry_before_sleeping_past_budget(self):
        from app.llm.recovery import RecoveryOrchestrator, RecoveryPolicy

        provider = SequenceProvider([_error(ModelErrorCode.OVERLOADED)] * 5)
        sleeps: list[float] = []
        orchestrator = RecoveryOrchestrator(
            provider,
            policy=RecoveryPolicy(
                max_transient_retries=4,
                deadline_seconds=0.4,
                jitter_ratio=0,
            ),
            sleep=lambda seconds: sleeps.append(seconds),
            clock=lambda: 0.0,
        )

        with self.assertRaises(ModelError) as raised:
            await orchestrator.complete(_request())

        self.assertEqual(raised.exception.code, ModelErrorCode.OVERLOADED)
        self.assertEqual(raised.exception.attempt, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(sleeps, [])

    async def test_non_transient_failures_are_never_retried(self):
        from app.llm.recovery import RecoveryOrchestrator

        cases = [
            (ModelErrorCode.AUTHENTICATION, False),
            (ModelErrorCode.PERMANENT, False),
            (ModelErrorCode.CONTENT_POLICY, False),
            (ModelErrorCode.PROMPT_TOO_LONG, True),
        ]
        for code, retryable in cases:
            with self.subTest(code=code):
                provider = SequenceProvider([_error(code, retryable=retryable), _result()])
                with self.assertRaises(ModelError) as raised:
                    await RecoveryOrchestrator(provider).complete(_request())
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.attempt, 1)
                self.assertEqual(provider.calls, 1)

    async def test_retry_exhaustion_returns_last_typed_error_and_safe_events(self):
        from app.llm.recovery import RecoveryOrchestrator, RecoveryPolicy

        provider = SequenceProvider([_error(ModelErrorCode.TIMEOUT)] * 3)
        events = []
        clock = ManualClock()
        orchestrator = RecoveryOrchestrator(
            provider,
            policy=RecoveryPolicy(
                max_transient_retries=2,
                deadline_seconds=5,
                jitter_ratio=0,
            ),
            sleep=clock.sleep,
            clock=clock,
            on_event=events.append,
        )

        with self.assertRaises(ModelError) as raised:
            await orchestrator.complete(_request())

        self.assertEqual(raised.exception.attempt, 3)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(
            [event.kind for event in events],
            ["retry_scheduled", "retry_scheduled"],
        )
        self.assertTrue(all(event.request_id == "req-recovery" for event in events))
        self.assertTrue(all(not hasattr(event, "messages") for event in events))
