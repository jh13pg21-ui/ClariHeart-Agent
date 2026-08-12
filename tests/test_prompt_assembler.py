import unittest

from app.context.contracts import ContextPlan, ContextSection
from app.context.tokens import ConservativeEstimator


def _section(section_id: str, category: str, content: str, **kwargs) -> ContextSection:
    return ContextSection(
        id=section_id,
        category=category,
        content=content,
        token_count=ConservativeEstimator().count(content) + 6,
        loading_reason=kwargs.pop("loading_reason", "test"),
        **kwargs,
    )


def _plan(*sections: ContextSection) -> ContextPlan:
    tokens = sum(section.token_count for section in sections)
    return ContextPlan(
        request_id="req-assemble",
        agent_name="ResponseAgent",
        task_name="response_generation",
        sections=tuple(sections),
        actions=(),
        input_budget=2000,
        hard_limit=1900,
        target_tokens=1900,
        tokens_before=tokens,
        tokens_after=tokens,
        dropped_section_ids=(),
        plan_hash="plan-hash",
    )


def _request():
    from app.prompts.assembler import PromptRequest

    return PromptRequest(
        request_id="req-assemble",
        agent_name="ResponseAgent",
        task_name="response_generation",
        agent_prompt_id="agent.response",
        task_prompt_id="task.response_generation",
        mode="support",
        locale="zh-CN",
    )


class PromptAssemblerTests(unittest.TestCase):
    def _assembler(self):
        from app.prompts.assembler import PromptAssembler
        from app.prompts.registry import default_prompt_registry

        return PromptAssembler(default_prompt_registry(), ConservativeEstimator())

    def test_static_prefix_is_stable_when_rag_changes(self):
        first = self._assembler().assemble(
            _request(),
            _plan(
                _section("rag.top1", "rag", "材料 A", provenance_ids=("chunk-a",)),
                _section("user.current", "user", "问题", required=True),
            ),
        )
        second = self._assembler().assemble(
            _request(),
            _plan(
                _section("rag.top1", "rag", "材料 B", provenance_ids=("chunk-b",)),
                _section("user.current", "user", "问题", required=True),
            ),
        )

        self.assertEqual(
            first.manifest.static_prefix_hash,
            second.manifest.static_prefix_hash,
        )
        self.assertNotEqual(
            first.manifest.dynamic_context_hash,
            second.manifest.dynamic_context_hash,
        )

    def test_manifest_contains_hashes_and_counts_but_no_context_content(self):
        secret = "不应进入 Manifest 的原始记忆"
        assembled = self._assembler().assemble(
            _request(),
            _plan(
                _section("memory.profile", "memory", secret, provenance_ids=("mem-1",)),
                _section("user.current", "user", "你好", required=True),
            ),
        )

        manifest_text = repr(assembled.manifest)
        self.assertNotIn(secret, manifest_text)
        self.assertEqual(len(assembled.manifest.manifest_hash), 64)
        self.assertGreater(assembled.manifest.total_tokens, 0)
        self.assertEqual(
            assembled.context_section_ids,
            ("memory.profile", "user.current"),
        )

    def test_historical_messages_and_current_input_are_appended_last(self):
        assembled = self._assembler().assemble(
            _request(),
            _plan(
                _section(
                    "conversation.user.1",
                    "conversation",
                    "上一问",
                    message_role="user",
                ),
                _section(
                    "conversation.assistant.1",
                    "conversation",
                    "上一答",
                    message_role="assistant",
                ),
                _section("rag.top1", "rag", "背景"),
                _section("user.current", "user", "当前问题", required=True),
            ),
        )

        self.assertEqual(
            [(message.role, message.content) for message in assembled.messages[-3:]],
            [("user", "上一问"), ("assistant", "上一答"), ("user", "当前问题")],
        )

    def test_static_instruction_order_is_cache_stable(self):
        assembled = self._assembler().assemble(
            _request(),
            _plan(_section("user.current", "user", "问题", required=True)),
        )
        prefix = assembled.messages[0].content

        ordered_ids = [
            "global.identity",
            "global.safety_boundary",
            "global.privacy_boundary",
            "global.untrusted_context",
            "agent.response",
            "task.response_generation",
        ]
        positions = [prefix.index(prompt_id) for prompt_id in ordered_ids]
        self.assertEqual(positions, sorted(positions))
