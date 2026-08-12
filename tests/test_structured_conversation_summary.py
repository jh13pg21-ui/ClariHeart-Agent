import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, ConversationMemorySummary, UserAccount
from app.services.conversation_summary import (
    SUMMARY_STATUS_FALLBACK,
    SUMMARY_STATUS_LLM,
    ConversationSummaryService,
)


class FakeAi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("没有更多测试响应")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def settings(**overrides):
    values = {
        "memory_compaction_enabled": True,
        "memory_compaction_recent_messages": 8,
        "memory_summary_refresh_messages": 4,
        "memory_summary_max_chars": 500,
        "memory_summary_llm_enabled": True,
        "memory_summary_llm_attempts": 2,
        "memory_summary_max_source_messages": 40,
        "memory_summary_input_max_chars": 12000,
        "redis_memory_max_messages": 40,
        "ai_provider": "ollama",
        "ollama_model": "summary-test-model",
        "sensitive_data_encryption_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class StructuredConversationSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.user = UserAccount(username="student", display_name="学生", password_hash="hash")
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(public_id="summary-session", user_id=self.user.id, title="摘要测试")
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _add_messages(self, count, first_user="我最近在准备秋招，主要担心面试。"):
        rows = []
        for index in range(count):
            role = "USER" if index % 2 == 0 else "ASSISTANT"
            content = first_user if index == 0 else f"第{index + 1}条测试消息"
            row = ChatMessage(
                user_id=self.user.id,
                session_id=self.session.id,
                role=role,
                content=content,
            )
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        return rows

    def _valid_response(self, evidence_id):
        return json.dumps(
            {
                "schemaVersion": 1,
                "studentConcerns": [
                    {
                        "topic": "秋招",
                        "summary": "学生正在准备秋招并担心面试",
                        "evidenceMessageIds": [evidence_id],
                    }
                ],
                "preferences": [],
                "effectiveSupports": [],
                "unresolvedThreads": [
                    {
                        "summary": "后续需要继续梳理面试准备计划",
                        "evidenceMessageIds": [evidence_id],
                    }
                ],
                "safetyContinuity": {
                    "followUpNeeded": False,
                    "summary": "",
                    "evidenceMessageIds": [],
                },
            },
            ensure_ascii=False,
        )

    async def test_refresh_uses_llm_and_keeps_eight_recent_raw_messages(self):
        rows = self._add_messages(12)
        ai = FakeAi([self._valid_response(rows[0].id)])
        service = ConversationSummaryService(self.db, settings(), ai=ai)

        self.assertTrue(service.should_schedule_refresh(self.session, rows[-1]))
        record = await service.refresh_for_assistant_message(rows[-1].id)
        self.db.commit()

        self.assertIsNotNone(record)
        self.assertEqual(record.status, SUMMARY_STATUS_LLM)
        self.assertEqual(record.through_message_id, rows[3].id)
        self.assertEqual(record.source_message_count, 4)
        prompt, brief = service.load_prompt_history(self.session, [])
        self.assertEqual(len(prompt), 9)
        self.assertEqual(prompt[0].role, "system")
        self.assertIn("准备秋招", brief)
        self.assertEqual(prompt[1].content, rows[4].content)
        self.assertEqual(prompt[-1].content, rows[-1].content)

    async def test_invalid_model_output_falls_back_and_sanitizes_sensitive_data(self):
        rows = self._add_messages(12, "我的手机号是13800138000，最近压力很大。")
        ai = FakeAi(["不是JSON", "仍然不是JSON"])
        service = ConversationSummaryService(self.db, settings(), ai=ai)

        record = await service.refresh_for_assistant_message(rows[-1].id)
        self.db.commit()

        self.assertEqual(record.status, SUMMARY_STATUS_FALLBACK)
        self.assertEqual(len(ai.calls), 2)
        self.assertNotIn("13800138000", record.summary_json)
        self.assertIn("[已脱敏]", record.summary_json)

    async def test_unknown_evidence_id_is_rejected_and_uses_fallback(self):
        rows = self._add_messages(12)
        invalid = self._valid_response(999999)
        service = ConversationSummaryService(
            self.db,
            settings(memory_summary_llm_attempts=1),
            ai=FakeAi([invalid]),
        )

        record = await service.refresh_for_assistant_message(rows[-1].id)

        self.assertEqual(record.status, SUMMARY_STATUS_FALLBACK)
        self.assertIn("合法证据消息ID", record.last_error)

    async def test_substantive_dialogue_cannot_pass_as_empty_llm_summary(self):
        rows = self._add_messages(12)
        empty = json.dumps(
            {
                "schemaVersion": 1,
                "studentConcerns": [],
                "preferences": [],
                "effectiveSupports": [],
                "unresolvedThreads": [],
                "safetyContinuity": {
                    "followUpNeeded": False,
                    "summary": "",
                    "evidenceMessageIds": [],
                },
            }
        )
        service = ConversationSummaryService(
            self.db,
            settings(memory_summary_llm_attempts=1),
            ai=FakeAi([empty]),
        )

        record = await service.refresh_for_assistant_message(rows[-1].id)

        self.assertEqual(record.status, SUMMARY_STATUS_FALLBACK)
        self.assertIn("空摘要", record.last_error)

    async def test_explicit_preference_must_be_present_in_model_summary(self):
        rows = self._add_messages(12, "我更喜欢先给结论，再给三个步骤。")
        missing_preference = self._valid_response(rows[0].id)
        service = ConversationSummaryService(
            self.db,
            settings(memory_summary_llm_attempts=1),
            ai=FakeAi([missing_preference]),
        )

        record = await service.refresh_for_assistant_message(rows[-1].id)
        payload = json.loads(record.summary_json)

        self.assertEqual(record.status, SUMMARY_STATUS_FALLBACK)
        self.assertIn("preferences", record.last_error)
        self.assertTrue(payload["preferences"])

    async def test_high_risk_wording_is_only_retained_as_safety_continuity(self):
        rows = self._add_messages(12, "我有自残的想法。")
        ai = FakeAi([self._valid_response(rows[0].id)])
        service = ConversationSummaryService(self.db, settings(), ai=ai)

        record = await service.refresh_for_assistant_message(rows[-1].id)
        payload = json.loads(record.summary_json)

        self.assertTrue(payload["safetyContinuity"]["followUpNeeded"])
        self.assertNotIn("自残", json.dumps(payload, ensure_ascii=False))
        self.assertIn("安全信号", payload["safetyContinuity"]["summary"])


if __name__ == "__main__":
    unittest.main()
