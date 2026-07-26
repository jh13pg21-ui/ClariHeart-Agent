import json
import unittest
from types import SimpleNamespace

from app.core.enums import RiskLevel
from app.schemas.dtos import ChatRequest
from app.services.chat import ChatService
from app.services.output_safety import OutputSafetyDecision, OutputSafetyStatus


class FakeHarness:
    def __init__(self, outcome):
        self.outcome = outcome
        self.saved = []

    async def run(self, user, request):
        return self.outcome

    def save_assistant_message(self, user, session, content):
        self.saved.append(content)

    async def dispatch_tools(self, plan):
        return []


class ExplodingAi:
    async def complete(self, messages):
        raise AssertionError("ChatService 不得二次生成")

    async def stream(self, messages):
        raise AssertionError("ChatService 不得二次生成")
        yield ""


def parse_event(chunk):
    lines = chunk.strip().splitlines()
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


def outcome(risk):
    text = "已经审核通过的回复文本，可以安全发送。"
    return SimpleNamespace(
        session=SimpleNamespace(public_id="session"),
        response_text=text,
        risk_level=risk.value,
        output_safety=OutputSafetyDecision(OutputSafetyStatus.APPROVED, text, "approved"),
        tool_plan=SimpleNamespace(),
        report_id=None,
    )


class ChatDeliveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def _events(self, risk):
        service = ChatService.__new__(ChatService)
        service.ai = ExplodingAi()
        service.agent_harness = FakeHarness(outcome(risk))
        user = SimpleNamespace()
        chunks = [
            chunk
            async for chunk in service.stream_chat(user, ChatRequest(message="你好"))
        ]
        return [parse_event(chunk) for chunk in chunks], service.agent_harness

    async def test_low_risk_sends_reviewed_text_as_chunks_and_done(self):
        events, harness = await self._events(RiskLevel.LOW)

        names = [name for name, _ in events]
        self.assertGreater(names.count("token"), 1)
        self.assertEqual(names[-1], "done")
        self.assertEqual(harness.saved, ["已经审核通过的回复文本，可以安全发送。"])

    async def test_medium_risk_sends_reviewed_text_as_chunks_and_done(self):
        events, _ = await self._events(RiskLevel.MEDIUM)

        names = [name for name, _ in events]
        self.assertIn("token", names)
        self.assertEqual(names[-1], "done")

    async def test_high_risk_emits_one_reviewed_message_and_zero_tokens_then_done(self):
        events, _ = await self._events(RiskLevel.HIGH)

        names = [name for name, _ in events]
        self.assertEqual(names.count("token"), 0)
        self.assertEqual(names.count("message"), 1)
        self.assertEqual(names[-1], "done")


if __name__ == "__main__":
    unittest.main()
