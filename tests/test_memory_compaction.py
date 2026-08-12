import unittest
from types import SimpleNamespace

from app.schemas.dtos import AiMessage
from app.services.memory import (
    ConversationSummaryState,
    compact_history_for_prompt,
    summarize_history_for_memory,
)


def settings(**overrides):
    values = {
        "memory_compaction_enabled": True,
        "memory_compaction_recent_messages": 4,
        "memory_summary_max_chars": 220,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MemoryCompactionTests(unittest.TestCase):
    def test_summary_state_refreshes_only_after_four_new_messages(self):
        history = [
            AiMessage(
                role="user" if index % 2 == 0 else "assistant",
                content=f"消息{index}",
            )
            for index in range(12)
        ]
        summary_settings = settings(memory_compaction_recent_messages=8)
        state = ConversationSummaryState.from_history(history[:8], summary_settings)

        before_threshold = state.advance(history[8:10], summary_settings)
        after_threshold = before_threshold.advance(history[10:12], summary_settings)

        self.assertEqual(before_threshold.summary, "")
        self.assertEqual(len(before_threshold.tail), 10)
        self.assertNotEqual(after_threshold.summary, "")
        self.assertEqual(
            [message.content for message in after_threshold.tail],
            [f"消息{index}" for index in range(4, 12)],
        )

    def test_compaction_keeps_recent_messages_and_adds_internal_summary(self):
        history = [AiMessage(role="user" if i % 2 == 0 else "assistant", content=f"第{i}条消息 13800138000") for i in range(10)]

        compacted, brief = compact_history_for_prompt(history, settings(), "我最近还是睡不着")

        self.assertEqual(compacted[0].role, "system")
        self.assertIn("历史摘要", compacted[0].content)
        self.assertEqual(len(compacted), 5)
        self.assertTrue(compacted[-1].content.startswith("第9条消息"))
        self.assertNotIn("13800138000", brief)
        self.assertIn("[已脱敏]", brief)

    def test_compaction_can_be_disabled(self):
        history = [AiMessage(role="user", content="我最近压力很大")]

        compacted, brief = compact_history_for_prompt(history, settings(memory_compaction_enabled=False), "")

        self.assertEqual(compacted, history)
        self.assertIn("学生近期关注", brief)

    def test_summary_is_bounded(self):
        history = [AiMessage(role="user", content="压力" * 200)]

        brief = summarize_history_for_memory(history, max_chars=80)

        self.assertLessEqual(len(brief), 80)

    def test_saturated_summary_keeps_newest_information(self):
        summary_settings = settings(
            memory_compaction_recent_messages=2,
            memory_summary_refresh_messages=1,
            memory_summary_max_chars=120,
        )
        state = ConversationSummaryState(
            "很早以前的背景" * 30,
            (
                AiMessage(role="user", content="新的重要事项：明天参加后端面试"),
                AiMessage(role="assistant", content="旧回复"),
                AiMessage(role="assistant", content="旧回复二"),
            ),
        )

        advanced = state.advance([], summary_settings)

        self.assertLessEqual(len(advanced.summary), 120)
        self.assertIn("明天参加后端面试", advanced.summary)


if __name__ == "__main__":
    unittest.main()
