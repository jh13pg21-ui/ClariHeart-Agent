import json
import unittest

import httpx

from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.providers import OllamaProvider, OpenAICompatibleProvider
from app.schemas.dtos import AiMessage


def _request(provider: str, *, stream: bool = False) -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        agent_name="ResponseAgent",
        task_name="response_generation",
        risk_level=RiskLevel.LOW,
        messages=(AiMessage(role="user", content="你好"),),
        preferred_provider=provider,
        preferred_model="test-model",
        max_output_tokens=256,
        stream=stream,
        sanitized=True,
    )


class ModelProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_result_preserves_usage_done_reason_and_request_payload(self):
        captured = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "message": {"content": "本地完成"},
                    "done_reason": "stop",
                    "prompt_eval_count": 42,
                    "eval_count": 7,
                },
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OllamaProvider(client, "http://ollama.test", temperature=0.2)

        result = await provider.complete(_request("ollama"))

        self.assertEqual(result.text, "本地完成")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual((result.input_tokens, result.output_tokens), (42, 7))
        self.assertFalse(result.partial)
        self.assertEqual(captured[0]["model"], "test-model")
        self.assertEqual(captured[0]["options"]["num_predict"], 256)
        self.assertFalse(captured[0]["stream"])

    async def test_openai_result_preserves_finish_reason_usage_and_request_id(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer secret")
            return httpx.Response(
                200,
                headers={"x-request-id": "provider-request-1"},
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "云端截断"},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                },
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OpenAICompatibleProvider(
            client,
            "http://cloud.test/v1",
            "secret",
            temperature=0.2,
        )

        result = await provider.complete(_request("openai"))

        self.assertEqual(result.finish_reason, "length")
        self.assertTrue(result.partial)
        self.assertEqual(result.provider_request_id, "provider-request-1")
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 4))

    async def test_provider_maps_http_failure_to_typed_error(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(529, text="overloaded", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OllamaProvider(client, "http://ollama.test")

        with self.assertRaises(ModelError) as raised:
            await provider.complete(_request("ollama"))

        self.assertEqual(raised.exception.code, ModelErrorCode.OVERLOADED)

    async def test_malformed_success_payload_is_invalid_response(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": {}}, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OllamaProvider(client, "http://ollama.test")

        with self.assertRaises(ModelError) as raised:
            await provider.complete(_request("ollama"))

        self.assertEqual(raised.exception.code, ModelErrorCode.INVALID_RESPONSE)

    async def test_ollama_stream_emits_tokens_usage_and_done(self):
        lines = [
            json.dumps({"message": {"content": "你"}, "done": False}),
            json.dumps(
                {
                    "message": {"content": "好"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 5,
                    "eval_count": 2,
                }
            ),
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="\n".join(lines), request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OllamaProvider(client, "http://ollama.test")

        events = [event async for event in provider.stream(_request("ollama", stream=True))]

        self.assertEqual("".join(event.text for event in events if event.kind == "token"), "你好")
        self.assertEqual(events[-2].kind, "usage")
        self.assertEqual((events[-2].input_tokens, events[-2].output_tokens), (5, 2))
        self.assertEqual((events[-1].kind, events[-1].finish_reason), ("done", "stop"))


if __name__ == "__main__":
    unittest.main()

