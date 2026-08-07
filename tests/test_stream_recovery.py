import unittest

from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest, ModelStreamEvent
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.recovery import RecoveryPolicy


def _request(risk: RiskLevel = RiskLevel.LOW, *, sanitized: bool = True) -> ModelRequest:
    return ModelRequest(
        request_id="req-stream",
        agent_name="ResponseAgent",
        task_name="respond",
        risk_level=risk,
        messages=(),
        preferred_provider="ollama",
        preferred_model="local-model",
        max_output_tokens=512,
        stream=True,
        cloud_egress_allowed=True,
        sanitized=sanitized,
        context_section_ids=("system-core", "current-input"),
    )


def _token(text: str) -> ModelStreamEvent:
    return ModelStreamEvent(kind="token", request_id="req-stream", text=text)


def _completed(*tokens: str) -> list[ModelStreamEvent]:
    return [
        *[_token(token) for token in tokens],
        ModelStreamEvent(
            kind="usage",
            request_id="req-stream",
            input_tokens=10,
            output_tokens=4,
        ),
        ModelStreamEvent(kind="done", request_id="req-stream", finish_reason="stop"),
    ]


def _interrupted(*tokens: str) -> list[ModelStreamEvent | ModelError]:
    return [
        *[_token(token) for token in tokens],
        ModelError(
            code=ModelErrorCode.STREAM_INTERRUPTED,
            message="stream interrupted",
            retryable=True,
            provider="ollama",
            model="local-model",
        ),
    ]


class StreamingSequenceProvider:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest):
        index = self.calls
        self.calls += 1
        self.requests.append(request)
        for item in self.streams[index]:
            if isinstance(item, ModelError):
                raise item
            yield item


def _text(events: list[ModelStreamEvent]) -> str:
    return "".join(event.text for event in events if event.kind == "token")


class StreamRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, local, cloud=None, **kwargs):
        from app.llm.gateway import ModelGateway

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
            stream_release_chars=kwargs.pop("stream_release_chars", 256),
            **kwargs,
        )

    async def test_interruption_before_release_retries_without_duplicate_tokens(self):
        local = StreamingSequenceProvider(
            [_interrupted("半"), _completed("完整")]
        )

        events = [event async for event in self._gateway(local).stream(_request())]

        self.assertEqual(_text(events), "完整")
        self.assertEqual(local.calls, 2)
        self.assertEqual([event.kind for event in events][-2:], ["usage", "done"])

    async def test_high_risk_stream_is_buffered_and_never_falls_back_to_cloud(self):
        local = StreamingSequenceProvider(
            [_interrupted("敏感片段"), _interrupted("仍然失败")]
        )
        cloud = StreamingSequenceProvider([_completed("forbidden")])

        with self.assertRaises(ModelError) as raised:
            _ = [
                event
                async for event in self._gateway(local, cloud).stream(
                    _request(RiskLevel.HIGH)
                )
            ]

        self.assertEqual(raised.exception.code, ModelErrorCode.STREAM_INTERRUPTED)
        self.assertEqual(cloud.calls, 0)

    async def test_interruption_after_release_emits_reviewed_replacement(self):
        local = StreamingSequenceProvider(
            [_interrupted("已发布"), _completed("最终完整答案")]
        )
        replacements: list[str] = []

        events = [
            event
            async for event in self._gateway(
                local,
                stream_release_chars=2,
            ).stream(
                _request(),
                reviewer=lambda text, request: True,
                on_replace=replacements.append,
            )
        ]

        self.assertEqual(_text(events), "已发布")
        replace_events = [event for event in events if event.kind == "replace_required"]
        self.assertEqual([event.text for event in replace_events], ["最终完整答案"])
        self.assertEqual(replacements, ["最终完整答案"])
        self.assertEqual(events[-1].kind, "done")

    async def test_low_risk_can_fallback_to_cloud_after_stream_retry_exhaustion(self):
        local = StreamingSequenceProvider([_interrupted(), _interrupted()])
        cloud = StreamingSequenceProvider([_completed("云端恢复")])

        events = [event async for event in self._gateway(local, cloud).stream(_request())]

        self.assertEqual(_text(events), "云端恢复")
        self.assertEqual(cloud.calls, 1)
        self.assertEqual(cloud.requests[0].preferred_provider, "openai")

    async def test_unsanitized_stream_never_falls_back_to_cloud(self):
        local = StreamingSequenceProvider([_interrupted(), _interrupted()])
        cloud = StreamingSequenceProvider([_completed("forbidden")])

        with self.assertRaises(ModelError):
            _ = [
                event
                async for event in self._gateway(local, cloud).stream(
                    _request(sanitized=False)
                )
            ]

        self.assertEqual(cloud.calls, 0)

    async def test_high_risk_success_requires_full_response_reviewer(self):
        local = StreamingSequenceProvider([_completed("高风险", "完整答复")])
        reviewed: list[str] = []

        async def reviewer(text: str, request: ModelRequest) -> bool:
            reviewed.append(text)
            return True

        events = [
            event
            async for event in self._gateway(local).stream(
                _request(RiskLevel.HIGH),
                reviewer=reviewer,
            )
        ]

        self.assertEqual(reviewed, ["高风险完整答复"])
        self.assertEqual(_text(events), "高风险完整答复")
