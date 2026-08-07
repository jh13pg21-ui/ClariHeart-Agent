import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ModelCallTrace
from app.services.model_trace import ModelTraceEvent, SqlModelTraceSink
from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest, ModelResult
from app.llm.gateway import ModelGateway


class ModelTraceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_sink_persists_only_metadata_whitelist(self):
        sink = SqlModelTraceSink(self.db)
        sink.record(
            ModelTraceEvent(
                kind="success",
                request_id="request-1",
                agent_name="ResponseAgent",
                task_name="respond",
                provider="ollama",
                model="qwen",
                risk_level="LOW",
                prompt_manifest_hash="manifest-abc",
                context_plan_hash="plan-def",
                input_tokens=120,
                output_tokens=30,
                prompt="手机号13800138000",
                messages=[{"content": "不应保存"}],
            )
        )

        row = self.db.query(ModelCallTrace).one()
        serialized = json.dumps(
            {
                column.name: getattr(row, column.name)
                for column in ModelCallTrace.__table__.columns
            },
            ensure_ascii=False,
            default=str,
        )
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("不应保存", serialized)
        self.assertEqual(row.prompt_manifest_hash, "manifest-abc")
        self.assertEqual(row.context_plan_hash, "plan-def")
        self.assertEqual(row.input_tokens, 120)

    def test_sink_failure_never_escapes_to_model_request(self):
        self.db.close()

        SqlModelTraceSink(self.db).record(
            ModelTraceEvent(kind="start", request_id="request-2")
        )


class CapturingSink:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.events = []

    def record(self, event):
        if self.fail:
            raise RuntimeError("trace storage unavailable")
        self.events.append(event)


class SuccessfulProvider:
    async def complete(self, request):
        return ModelResult(
            text="ok",
            provider="ollama",
            model="qwen",
            finish_reason="stop",
            input_tokens=10,
            output_tokens=2,
            latency_ms=5,
            request_id=request.request_id,
        )


class GatewayModelTraceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request():
        return ModelRequest(
            request_id="gateway-trace",
            agent_name="ResponseAgent",
            task_name="respond",
            risk_level=RiskLevel.LOW,
            messages=(),
            preferred_provider="ollama",
            preferred_model="qwen",
            max_output_tokens=100,
            prompt_manifest_hash="manifest",
            context_plan_hash="plan",
            context_section_ids=("global.safety", "user.current"),
        )

    async def test_gateway_emits_start_and_success_metadata(self):
        sink = CapturingSink()
        result = await ModelGateway(
            {"ollama": SuccessfulProvider()},
            trace_sink=sink,
        ).complete(self._request())

        self.assertEqual(result.text, "ok")
        self.assertEqual([event.kind for event in sink.events], ["start", "success"])
        self.assertEqual(sink.events[-1].prompt_manifest_hash, "manifest")
        self.assertEqual(sink.events[-1].context_plan_hash, "plan")
        self.assertEqual(sink.events[-1].context_section_count, 2)

    async def test_trace_sink_failure_does_not_fail_model_call(self):
        result = await ModelGateway(
            {"ollama": SuccessfulProvider()},
            trace_sink=CapturingSink(fail=True),
        ).complete(self._request())

        self.assertEqual(result.text, "ok")
