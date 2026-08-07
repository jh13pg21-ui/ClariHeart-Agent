import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.context.contracts import ContextEnvelope, ContextSection
from app.context.planner import ContextPlanner, ContextPlanningError
from app.context.tokens import ConservativeEstimator
from app.llm.capabilities import ModelCapabilities
from app.models.entities import Base


ALLOWLIST = [
    "global.identity",
    "global.safety",
    "agent.contract",
    "conversation.summary",
    "conversation.latest_turn",
    "skill.mandatory",
    "rag.top1",
    "user.current",
]


def _section(section_id: str, *, required: bool = False, content: str = "必要内容"):
    return ContextSection(
        id=section_id,
        category=section_id.split(".")[0],
        content=content,
        priority=90,
        required=required,
        provenance_ids=(f"source:{section_id}",),
        loading_reason="test",
    )


def _capabilities(window: int = 2000):
    return ModelCapabilities(
        provider="ollama",
        model="local-model",
        context_window=window,
        default_output_tokens=200,
        maximum_output_tokens=500,
        cloud=False,
    )


def _engine():
    from app.context.compaction import CompactionEngine

    planner = ContextPlanner(
        ConservativeEstimator(),
        reserve_tokens=100,
        margin_ratio=0,
        category_budget_ratios={category: 1.0 for category in {
            "global", "agent", "conversation", "skill", "rag", "user"
        }},
    )
    return CompactionEngine(planner)


class ReactiveContextCompactionTests(unittest.TestCase):
    def test_reactive_plan_keeps_only_emergency_allowlist_in_fixed_order(self):
        sections = [
            *[_section(section_id, required=section_id in {"global.safety", "user.current"}) for section_id in ALLOWLIST],
            *[_section(f"rag.extra.{index}", content="冗余材料" * 100) for index in range(8)],
            _section("memory.profile", content="长期记忆" * 100),
        ]
        envelope = ContextEnvelope(
            request_id="req-reactive",
            agent_name="ResponseAgent",
            task_name="response_generation",
            sections=tuple(sections),
        )

        plan = _engine().reactive_plan(envelope, _capabilities())

        self.assertEqual([section.id for section in plan.sections], ALLOWLIST)
        self.assertTrue(plan.reactive)
        self.assertEqual(plan.reason, "prompt_too_long")

    def test_reactive_plan_cannot_be_applied_twice(self):
        envelope = ContextEnvelope(
            request_id="req-reactive",
            agent_name="ResponseAgent",
            task_name="response_generation",
            sections=tuple(_section(section_id) for section_id in ALLOWLIST),
            reactive=True,
        )

        with self.assertRaises(ContextPlanningError) as raised:
            _engine().reactive_plan(envelope, _capabilities())

        self.assertEqual(raised.exception.code, "CONTEXT_UNRECOVERABLE")

    def test_required_emergency_content_that_still_overflows_is_unrecoverable(self):
        envelope = ContextEnvelope(
            request_id="req-reactive",
            agent_name="ResponseAgent",
            task_name="response_generation",
            sections=(
                _section("global.safety", required=True, content="安全" * 1000),
                _section("user.current", required=True, content="当前" * 1000),
            ),
        )

        with self.assertRaises(ContextPlanningError) as raised:
            _engine().reactive_plan(envelope, _capabilities(window=500))

        self.assertEqual(raised.exception.code, "CONTEXT_UNRECOVERABLE")

    def test_compaction_audit_stores_metadata_without_section_content(self):
        from app.context.compaction import record_compaction
        from app.models.entities import ContextCompactionRecord

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, future=True)
        envelope = ContextEnvelope(
            request_id="req-audit",
            agent_name="ResponseAgent",
            task_name="response_generation",
            sections=tuple(_section(section_id) for section_id in ALLOWLIST),
        )
        plan = _engine().reactive_plan(envelope, _capabilities())

        with SessionLocal() as db:
            record = record_compaction(db, plan, "manifest-hash")
            db.commit()
            loaded = db.get(ContextCompactionRecord, record.id)

        serialized = json.dumps(
            {
                "layers": loaded.layers_json,
                "reason": loaded.reason,
                "watermark": loaded.watermark,
            },
            ensure_ascii=False,
        )
        self.assertNotIn("必要内容", serialized)
        self.assertEqual(loaded.request_id, "req-audit")
        self.assertEqual(loaded.manifest_hash, "manifest-hash")
