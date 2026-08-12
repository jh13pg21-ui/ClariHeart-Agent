import unittest

from app.context.tokens import ConservativeEstimator
from app.llm.capabilities import ModelCapabilities


def _capabilities(context_window: int = 700) -> ModelCapabilities:
    return ModelCapabilities(
        provider="ollama",
        model="tiny-local",
        context_window=context_window,
        default_output_tokens=100,
        maximum_output_tokens=200,
        cloud=False,
    )


def _section(
    section_id: str,
    category: str,
    content: str,
    *,
    required: bool = False,
    priority: int = 50,
    provenance_ids=(),
):
    from app.context.contracts import ContextSection

    return ContextSection(
        id=section_id,
        category=category,
        content=content,
        required=required,
        priority=priority,
        provenance_ids=tuple(provenance_ids),
        loading_reason="test",
    )


def _envelope(*sections, reactive: bool = False):
    from app.context.contracts import ContextEnvelope

    return ContextEnvelope(
        request_id="req-plan",
        agent_name="ResponseAgent",
        task_name="response_generation",
        sections=tuple(sections),
        reactive=reactive,
    )


def _planner():
    from app.context.planner import ContextPlanner

    return ContextPlanner(
        ConservativeEstimator(),
        reserve_tokens=50,
        margin_ratio=0,
        category_budget_ratios={
            "global": 1.0,
            "user": 1.0,
            "conversation": 0.6,
            "rag": 0.4,
            "memory": 0.4,
            "skill": 0.3,
        },
    )


class ContextPlannerTests(unittest.TestCase):
    def test_required_sections_survive_budget_pressure(self):
        sections = [
            _section("global.safety", "global", "安全规则" * 20, required=True, priority=100),
            _section("user.current", "user", "当前问题" * 15, required=True, priority=100),
            *[
                _section(f"rag.{index}", "rag", "检索材料" * 80, priority=20 - index)
                for index in range(4)
            ],
        ]

        plan = _planner().plan(_envelope(*sections), _capabilities(), 100)

        selected_ids = {section.id for section in plan.sections}
        self.assertTrue({"global.safety", "user.current"}.issubset(selected_ids))
        self.assertLessEqual(plan.tokens_after, int(plan.input_budget * 0.95))
        self.assertTrue(plan.compacted)

    def test_duplicate_provenance_and_content_is_kept_once(self):
        duplicate = _section(
            "rag.second",
            "rag",
            "同一文档片段",
            provenance_ids=("chunk-1",),
        )
        plan = _planner().plan(
            _envelope(
                _section("user.current", "user", "问题", required=True),
                _section(
                    "rag.first",
                    "rag",
                    "同一文档片段",
                    provenance_ids=("chunk-1",),
                ),
                duplicate,
            ),
            _capabilities(),
            100,
        )

        self.assertEqual(
            sum("chunk-1" in section.provenance_ids for section in plan.sections),
            1,
        )
        self.assertIn("rag.second", plan.dropped_section_ids)

    def test_same_envelope_produces_same_plan(self):
        envelope = _envelope(
            _section("global.safety", "global", "安全", required=True),
            _section("user.current", "user", "问题", required=True),
            _section("memory.2", "memory", "较低优先", priority=10),
            _section("memory.1", "memory", "较高优先", priority=20),
        )

        self.assertEqual(
            _planner().plan(envelope, _capabilities(), 100),
            _planner().plan(envelope, _capabilities(), 100),
        )

    def test_structured_summary_replaces_older_conversation_sections_under_pressure(self):
        plan = _planner().plan(
            _envelope(
                _section("global.safety", "global", "安全", required=True),
                _section("user.current", "user", "当前", required=True),
                _section("conversation.summary", "conversation", "结构化摘要" * 20, priority=90),
                _section("conversation.history.1", "conversation", "很旧对话" * 80, priority=10),
                _section("conversation.latest_turn", "conversation", "最近一轮" * 20, priority=95),
                _section("rag.large", "rag", "知识" * 150, priority=40),
            ),
            _capabilities(context_window=500),
            100,
        )

        ids = {section.id for section in plan.sections}
        self.assertIn("conversation.summary", ids)
        self.assertIn("conversation.latest_turn", ids)
        self.assertNotIn("conversation.history.1", ids)

    def test_required_content_larger_than_hard_limit_fails_closed(self):
        from app.context.planner import ContextPlanningError

        envelope = _envelope(
            _section("global.safety", "global", "必须保留" * 500, required=True)
        )

        with self.assertRaises(ContextPlanningError) as raised:
            _planner().plan(envelope, _capabilities(context_window=300), 100)

        self.assertEqual(raised.exception.code, "INPUT_TOO_LARGE")

    def test_selected_optional_sections_follow_priority_then_stable_id(self):
        plan = _planner().plan(
            _envelope(
                _section("user.current", "user", "问题", required=True),
                _section("rag.b", "rag", "b" * 300, priority=10),
                _section("rag.a", "rag", "a" * 300, priority=20),
                _section("rag.c", "rag", "c" * 300, priority=20),
            ),
            _capabilities(context_window=400),
            100,
        )

        selected_optional = [section.id for section in plan.sections if not section.required]
        self.assertEqual(selected_optional, ["rag.a"])
