import unittest

from app.core.enums import RiskLevel
from app.llm.capabilities import ModelCapabilities
from app.llm.contracts import ModelRequest, ModelResult
from app.schemas.dtos import AiMessage


def _request(max_output_tokens: int = 512) -> ModelRequest:
    return ModelRequest(
        request_id="req-output",
        agent_name="ResponseAgent",
        task_name="respond",
        risk_level=RiskLevel.LOW,
        messages=(AiMessage(role="user", content="请生成完整方案"),),
        preferred_provider="ollama",
        preferred_model="local-model",
        max_output_tokens=max_output_tokens,
    )


def _result(
    text: str,
    *,
    finish_reason: str = "stop",
    output_tokens: int = 10,
) -> ModelResult:
    return ModelResult(
        text=text,
        provider="ollama",
        model="local-model",
        finish_reason=finish_reason,
        input_tokens=20,
        output_tokens=output_tokens,
        latency_ms=10,
        request_id="req-output",
        partial=finish_reason in {"length", "max_tokens"},
    )


class CapturingProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return self.outcomes[len(self.requests) - 1]


def _capabilities(maximum: int = 2048) -> ModelCapabilities:
    return ModelCapabilities(
        provider="ollama",
        model="local-model",
        context_window=32768,
        default_output_tokens=512,
        maximum_output_tokens=maximum,
        cloud=False,
    )


class OutputTokenRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_resolves_provider_capabilities_for_output_recovery(self):
        from app.llm.gateway import ModelGateway

        class Registry:
            def for_model(self, provider: str, model: str) -> ModelCapabilities:
                return _capabilities(maximum=2048)

        provider = CapturingProvider(
            [_result("半截", finish_reason="length"), _result("完整")]
        )
        gateway = ModelGateway(
            {"ollama": provider},
            capabilities_registry=Registry(),
        )

        result = await gateway.complete(_request())

        self.assertEqual(result.text, "完整")
        self.assertEqual(provider.requests[1].max_output_tokens, 1024)

    async def test_first_truncation_escalates_without_appending_partial_text(self):
        from app.llm.recovery import RecoveryOrchestrator

        provider = CapturingProvider(
            [_result("不应拼接的半截", finish_reason="length"), _result("完整答案")]
        )

        result = await RecoveryOrchestrator(
            provider,
            capabilities=_capabilities(),
        ).complete(_request())

        self.assertEqual(result.text, "完整答案")
        self.assertEqual(provider.requests[1].messages, provider.requests[0].messages)
        self.assertGreater(provider.requests[1].max_output_tokens, 512)
        self.assertEqual(result.input_tokens, 40)
        self.assertEqual(result.output_tokens, 20)

    async def test_truncation_at_ceiling_uses_reviewable_bounded_continuation(self):
        from app.llm.recovery import RecoveryOrchestrator

        provider = CapturingProvider(
            [_result("第一部分。", finish_reason="length"), _result("第二部分。")]
        )

        result = await RecoveryOrchestrator(
            provider,
            capabilities=_capabilities(maximum=512),
        ).complete(_request(max_output_tokens=512))

        self.assertEqual(result.text, "第一部分。第二部分。")
        self.assertFalse(result.partial)
        continuation = provider.requests[1]
        self.assertEqual(continuation.messages[-2].role, "assistant")
        self.assertEqual(continuation.messages[-2].content, "第一部分。")
        self.assertIn("不要重复", continuation.messages[-1].content)

    async def test_diminishing_continuation_returns_explicit_partial(self):
        from app.llm.recovery import RecoveryOrchestrator, RecoveryPolicy

        provider = CapturingProvider(
            [
                _result("a" * 256, finish_reason="length"),
                _result("补充太短", finish_reason="length"),
                _result("不应调用", finish_reason="stop"),
            ]
        )

        result = await RecoveryOrchestrator(
            provider,
            capabilities=_capabilities(maximum=512),
            policy=RecoveryPolicy(max_continuations=2),
        ).complete(_request(max_output_tokens=512))

        self.assertTrue(result.partial)
        self.assertEqual(result.finish_reason, "recovery_limit")
        self.assertEqual(result.text, "a" * 256 + "补充太短")
        self.assertEqual(len(provider.requests), 2)

    async def test_repeated_continuation_is_not_duplicated(self):
        from app.llm.recovery import RecoveryOrchestrator

        repeated = "重复内容" * 40
        provider = CapturingProvider(
            [
                _result("开头", finish_reason="length"),
                _result(repeated, finish_reason="length"),
                _result(repeated, finish_reason="length"),
            ]
        )

        result = await RecoveryOrchestrator(
            provider,
            capabilities=_capabilities(maximum=512),
        ).complete(_request(max_output_tokens=512))

        self.assertEqual(result.text, "开头" + repeated)
        self.assertEqual(result.finish_reason, "recovery_limit")
        self.assertEqual(result.text.count(repeated), 1)

    async def test_zero_continuation_budget_returns_original_partial(self):
        from app.llm.recovery import RecoveryOrchestrator, RecoveryPolicy

        provider = CapturingProvider([_result("仅有半截", finish_reason="length")])
        result = await RecoveryOrchestrator(
            provider,
            capabilities=_capabilities(maximum=512),
            policy=RecoveryPolicy(max_continuations=0),
        ).complete(_request(max_output_tokens=512))

        self.assertEqual(result.text, "仅有半截")
        self.assertTrue(result.partial)
        self.assertEqual(result.finish_reason, "recovery_limit")
