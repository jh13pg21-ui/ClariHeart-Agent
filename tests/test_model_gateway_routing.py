import unittest
from dataclasses import replace

from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest, ModelResult
from app.llm.errors import ModelError, ModelErrorCode


def _request(
    risk: RiskLevel = RiskLevel.LOW,
    *,
    sanitized: bool = True,
    cloud: bool = True,
    provider: str = "ollama",
) -> ModelRequest:
    return ModelRequest(
        request_id="req-route",
        agent_name="ResponseAgent",
        task_name="respond",
        risk_level=risk,
        messages=(),
        preferred_provider=provider,
        preferred_model="local-model" if provider == "ollama" else "cloud-model",
        max_output_tokens=512,
        cloud_egress_allowed=cloud,
        sanitized=sanitized,
        context_section_ids=("system-core", "current-input"),
    )


def _result(text: str, provider: str = "ollama") -> ModelResult:
    return ModelResult(
        text=text,
        provider=provider,
        model=f"{provider}-model",
        finish_reason="stop",
        input_tokens=10,
        output_tokens=3,
        latency_ms=5,
        request_id="req-route",
    )


def _error(code: ModelErrorCode, *, retryable: bool = True) -> ModelError:
    return ModelError(
        code=code,
        message=code.value,
        retryable=retryable,
        provider="ollama",
        model="local-model",
    )


class SequenceProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, ModelError):
            raise outcome
        return outcome


class ModelGatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, local, cloud=None, events=None):
        from app.llm.gateway import ModelGateway
        from app.llm.recovery import RecoveryPolicy

        providers = {"ollama": local}
        if cloud is not None:
            providers["openai"] = cloud
        return ModelGateway(
            providers,
            cloud_provider="openai",
            cloud_model="cloud-model",
            recovery_policy=RecoveryPolicy(
                max_transient_retries=2,
                deadline_seconds=5,
                base_delay_seconds=0,
                jitter_ratio=0,
            ),
            on_event=None if events is None else events.append,
        )

    async def test_low_risk_switches_to_cloud_after_local_overload_budget(self):
        local = SequenceProvider([_error(ModelErrorCode.OVERLOADED)] * 3)
        cloud = SequenceProvider([_result("cloud", provider="openai")])
        events = []

        result = await self._gateway(local, cloud, events).complete(_request())

        self.assertEqual(result.provider, "openai")
        self.assertEqual(local.calls, 3)
        self.assertEqual(cloud.calls, 1)
        self.assertEqual(cloud.requests[0].preferred_provider, "openai")
        self.assertEqual(cloud.requests[0].preferred_model, "cloud-model")
        self.assertEqual(events[-1].kind, "route_fallback")

    async def test_high_risk_never_invokes_cloud(self):
        local = SequenceProvider([_error(ModelErrorCode.OVERLOADED)] * 3)
        cloud = SequenceProvider([_result("forbidden", provider="openai")])

        with self.assertRaises(ModelError) as raised:
            await self._gateway(local, cloud).complete(_request(RiskLevel.HIGH))

        self.assertEqual(raised.exception.code, ModelErrorCode.OVERLOADED)
        self.assertEqual(cloud.calls, 0)

    async def test_unsanitized_request_never_invokes_cloud(self):
        local = SequenceProvider([_error(ModelErrorCode.NETWORK)] * 3)
        cloud = SequenceProvider([_result("forbidden", provider="openai")])

        with self.assertRaises(ModelError):
            await self._gateway(local, cloud).complete(_request(sanitized=False))

        self.assertEqual(cloud.calls, 0)

    async def test_permanent_and_prompt_too_long_errors_do_not_change_route(self):
        for code, retryable in (
            (ModelErrorCode.AUTHENTICATION, False),
            (ModelErrorCode.PERMANENT, False),
            (ModelErrorCode.PROMPT_TOO_LONG, True),
        ):
            with self.subTest(code=code):
                local = SequenceProvider([_error(code, retryable=retryable)])
                cloud = SequenceProvider([_result("forbidden", provider="openai")])
                with self.assertRaises(ModelError) as raised:
                    await self._gateway(local, cloud).complete(_request())
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(cloud.calls, 0)

    async def test_direct_cloud_route_is_denied_before_provider_call(self):
        local = SequenceProvider([_result("unused")])
        cloud = SequenceProvider([_result("forbidden", provider="openai")])

        with self.assertRaises(ModelError) as raised:
            await self._gateway(local, cloud).complete(
                _request(RiskLevel.HIGH, provider="openai")
            )

        self.assertEqual(raised.exception.code, ModelErrorCode.CLOUD_EGRESS_DENIED)
        self.assertEqual(cloud.calls, 0)

    async def test_unknown_primary_provider_is_a_typed_permanent_error(self):
        local = SequenceProvider([_result("unused")])
        request = replace(
            _request(cloud=False),
            preferred_provider="missing",
            preferred_model="missing-model",
        )

        with self.assertRaises(ModelError) as raised:
            await self._gateway(local).complete(request)

        self.assertEqual(raised.exception.code, ModelErrorCode.PERMANENT)
        self.assertIn("missing", raised.exception.message)
