import inspect
import unittest
from types import SimpleNamespace

import httpx

from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


def settings(provider: str = "mock"):
    return SimpleNamespace(
        ai_provider=provider,
        ai_temperature=0.2,
        ai_max_tokens=64,
        ollama_base_url="http://ollama.test",
        ollama_model="test-ollama",
        openai_base_url="http://openai.test/v1",
        openai_api_key="secret",
        openai_model="test-openai",
    )


class AsyncAiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_is_awaitable_for_mock_provider(self):
        pending = AiClient(settings()).complete([AiMessage(role="user", content="你好")])

        self.assertTrue(inspect.isawaitable(pending))
        self.assertTrue(await pending)

    async def test_stream_is_an_async_iterator_for_mock_provider(self):
        stream = AiClient(settings()).stream([AiMessage(role="user", content="你好")])

        self.assertTrue(hasattr(stream, "__aiter__"))
        self.assertTrue("".join([chunk async for chunk in stream]))

    async def test_ollama_complete_uses_injected_shared_async_client(self):
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"message": {"content": "异步完成"}})

        shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            client = AiClient(settings("ollama"), http_client=shared)

            result = await client.complete([AiMessage(role="user", content="测试")])

            self.assertEqual(result, "异步完成")
            self.assertIs(client.http_client, shared)
            self.assertEqual(len(requests), 1)
        finally:
            await shared.aclose()


if __name__ == "__main__":
    unittest.main()
