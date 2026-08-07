import json
import unittest

from app.context.contracts import ContextSection
from app.context.tokens import ConservativeEstimator
from app.core.enums import RiskLevel
from app.llm.contracts import ModelResult


def _result(text: str) -> ModelResult:
    return ModelResult(
        text=text,
        provider="ollama",
        model="local-model",
        finish_reason="stop",
        input_tokens=20,
        output_tokens=10,
        latency_ms=5,
        request_id="req-compact",
    )


def _section(section_id: str, category: str, content: str, source: str, **kwargs):
    return ContextSection(
        id=section_id,
        category=category,
        content=content,
        provenance_ids=(source,),
        loading_reason="test",
        token_count=ConservativeEstimator().count(content) + 6,
        **kwargs,
    )


class FakeGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        outcome = self.outcomes[len(self.requests) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SectionCompactorTests(unittest.IsolatedAsyncioTestCase):
    def _compactor(self, gateway):
        from app.context.section_compactor import SectionCompactor

        return SectionCompactor(
            gateway,
            ConservativeEstimator(),
            local_provider="ollama",
            local_model="local-model",
        )

    async def test_section_summary_preserves_only_valid_source_ids(self):
        gateway = FakeGateway(
            [_result(json.dumps({"summary": "考试安排", "sourceIds": ["chunk-1", "invented"]}))]
        )
        sections = (
            _section("rag.1", "rag", "考试安排原文" * 30, "chunk-1"),
            _section("rag.2", "rag", "补充材料" * 30, "chunk-2"),
        )

        compacted = await self._compactor(gateway).compact(
            sections,
            target_tokens=80,
            risk_level=RiskLevel.LOW,
        )

        self.assertEqual(compacted[0].provenance_ids, ("chunk-1",))
        self.assertNotIn("invented", compacted[0].provenance_ids)
        self.assertEqual(compacted[0].content, "考试安排")

    async def test_invalid_section_summary_uses_deterministic_trimming(self):
        sections = (
            _section("memory.1", "memory", "长期记忆甲" * 100, "mem-1"),
            _section("memory.2", "memory", "长期记忆乙" * 100, "mem-2"),
        )
        gateway = FakeGateway([_result("not-json")])

        compacted = await self._compactor(gateway).compact(
            sections,
            target_tokens=80,
            risk_level=RiskLevel.MEDIUM,
        )

        self.assertLessEqual(sum(section.token_count for section in compacted), 80)
        self.assertTrue(all(section.provenance_ids for section in compacted))
        self.assertEqual(compacted[0].provenance_ids, ("mem-1", "mem-2"))

    async def test_high_risk_compaction_never_authorizes_cloud(self):
        gateway = FakeGateway(
            [_result(json.dumps({"summary": "安全摘要", "sourceIds": ["mem-1"]}))]
        )

        await self._compactor(gateway).compact(
            (_section("memory.1", "memory", "内容" * 100, "mem-1"),),
            target_tokens=50,
            risk_level=RiskLevel.HIGH,
        )

        request = gateway.requests[0]
        self.assertFalse(request.cloud_egress_allowed)
        self.assertFalse(request.sanitized)

    async def test_required_section_is_never_sent_for_compaction_or_modified(self):
        required = _section(
            "global.safety",
            "global",
            "安全规则",
            "safety-v1",
            required=True,
        )
        optional = _section("rag.1", "rag", "材料" * 100, "chunk-1")
        gateway = FakeGateway(
            [_result(json.dumps({"summary": "材料摘要", "sourceIds": ["chunk-1"]}))]
        )

        compacted = await self._compactor(gateway).compact(
            (required, optional),
            target_tokens=70,
            risk_level=RiskLevel.LOW,
        )

        self.assertIn(required, compacted)
        serialized = " ".join(message.content for message in gateway.requests[0].messages)
        self.assertNotIn("安全规则", serialized)

    async def test_sections_from_different_categories_are_not_merged(self):
        gateway = FakeGateway(
            [
                _result(json.dumps({"summary": "记忆摘要", "sourceIds": ["mem-1"]})),
                _result(json.dumps({"summary": "知识摘要", "sourceIds": ["chunk-1"]})),
            ]
        )
        sections = (
            _section("memory.1", "memory", "记忆" * 100, "mem-1"),
            _section("rag.1", "rag", "知识" * 100, "chunk-1"),
        )

        compacted = await self._compactor(gateway).compact(
            sections,
            target_tokens=100,
            risk_level=RiskLevel.LOW,
        )

        self.assertEqual({section.category for section in compacted}, {"memory", "rag"})
        self.assertEqual(len(gateway.requests), 2)
