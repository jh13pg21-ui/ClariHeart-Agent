import unittest
from types import SimpleNamespace

from app.core.enums import RiskLevel
from app.llm.contracts import ModelResult, ModelStreamEvent
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


def _settings(provider: str = "ollama"):
    return SimpleNamespace(
        ai_provider=provider,
        ai_temperature=0.2,
        ai_max_tokens=128,
        ollama_base_url="http://ollama.test",
        ollama_model="local-model",
        openai_base_url="http://openai.test/v1",
        openai_api_key="secret",
        openai_model="cloud-model",
        model_cloud_fallback_enabled=True,
    )


def _result(text: str = "ok") -> ModelResult:
    return ModelResult(
        text=text,
        provider="ollama",
        model="local-model",
        finish_reason="stop",
        input_tokens=4,
        output_tokens=2,
        latency_ms=3,
        request_id="gateway-id",
    )


class FakeGateway:
    def __init__(self, result: ModelResult):
        self.result = result
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.result

    async def stream(self, request, **kwargs):
        self.requests.append(request)
        yield ModelStreamEvent(kind="token", request_id=request.request_id, text="第一段")
        yield ModelStreamEvent(kind="token", request_id=request.request_id, text="第二段")
        yield ModelStreamEvent(kind="done", request_id=request.request_id, finish_reason="stop")


class GatewayCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_uses_gateway_and_preserves_string_api(self):
        gateway = FakeGateway(_result("兼容答案"))
        client = AiClient(_settings(), gateway=gateway)

        text = await client.complete([AiMessage(role="user", content="你好")])

        self.assertEqual(text, "兼容答案")
        self.assertEqual(gateway.requests[0].agent_name, "legacy")
        self.assertEqual(gateway.requests[0].task_name, "legacy")
        self.assertEqual(gateway.requests[0].risk_level, RiskLevel.LOW)

    async def test_complete_result_preserves_metadata_and_sanitizes_cloud_request(self):
        gateway = FakeGateway(_result("结构化结果"))
        client = AiClient(_settings(), gateway=gateway)

        result = await client.complete_result(
            [AiMessage(role="user", content="我叫张三，手机 13800138000")],
            agent_name="ResponseAgent",
            task_name="response-1",
            risk_level=RiskLevel.MEDIUM,
            cloud_egress_allowed=True,
            context_section_ids=("current-input",),
        )

        request = gateway.requests[0]
        self.assertEqual(result.text, "结构化结果")
        self.assertTrue(request.cloud_egress_allowed)
        self.assertTrue(request.sanitized)
        self.assertNotIn("张三", request.messages[0].content)
        self.assertNotIn("13800138000", request.messages[0].content)
        self.assertEqual(request.context_section_ids, ("current-input",))

    async def test_high_risk_ignores_cloud_fallback_request(self):
        gateway = FakeGateway(_result())
        client = AiClient(_settings(), gateway=gateway)

        await client.complete_result(
            [AiMessage(role="user", content="敏感内容")],
            risk_level=RiskLevel.HIGH,
            cloud_egress_allowed=True,
        )

        request = gateway.requests[0]
        self.assertFalse(request.cloud_egress_allowed)
        self.assertFalse(request.sanitized)

    async def test_stream_preserves_legacy_string_chunks(self):
        gateway = FakeGateway(_result())
        client = AiClient(_settings(), gateway=gateway)

        chunks = [
            chunk
            async for chunk in client.stream(
                [AiMessage(role="user", content="你好")],
                agent_name="ResponseAgent",
            )
        ]

        self.assertEqual(chunks, ["第一段", "第二段"])
        self.assertTrue(gateway.requests[0].stream)
