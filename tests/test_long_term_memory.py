import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, LongTermMemory, UserAccount
from app.services.long_term_memory import LongTermMemoryService


class FakeAi:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        return self.response


class LongTermMemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.user = UserAccount(
            username="student",
            display_name="学生",
            password_hash="hash",
        )
        self.other = UserAccount(
            username="other",
            display_name="其他学生",
            password_hash="hash",
        )
        self.db.add_all([self.user, self.other])
        self.db.flush()
        self.session = ChatSession(
            public_id="session-a",
            title="会话",
            user_id=self.user.id,
        )
        self.db.add(self.session)
        self.db.commit()
        self.settings = SimpleNamespace(
            long_term_memory_enabled=True,
            long_term_memory_max_items=200,
            long_term_memory_relevant_items=5,
            long_term_memory_extract_messages=10,
        )

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_upsert_is_user_scoped_and_deduplicates_same_memory(self):
        service = LongTermMemoryService(self.db, self.settings)

        first = service.upsert(
            self.user.id,
            self.session.id,
            "PREFERENCE",
            "回复风格",
            "学生偏好简洁回复",
            "学生希望回答先给结论，再给两三条建议。",
        )
        second = service.upsert(
            self.user.id,
            self.session.id,
            "PREFERENCE",
            "回复风格",
            "学生偏好简洁回复",
            "学生希望回答先给结论，再给两三条建议。",
        )
        service.upsert(
            self.other.id,
            None,
            "PREFERENCE",
            "回复风格",
            "其他学生的偏好",
            "其他学生喜欢详细回答。",
        )
        self.db.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(service.list_for_user(self.user.id)), 1)
        self.assertNotIn(
            "其他学生",
            "\n".join(item.body for item in service.list_for_user(self.user.id)),
        )

    def test_sensitive_or_high_risk_content_is_not_stored(self):
        service = LongTermMemoryService(self.db, self.settings)

        phone = service.upsert(
            self.user.id,
            self.session.id,
            "PROFILE",
            "联系方式",
            "手机号",
            "我的手机号是 13800138000。",
        )
        crisis = service.upsert(
            self.user.id,
            self.session.id,
            "CONTEXT",
            "危险表达",
            "当前危险情况",
            "我现在不想活了。",
        )

        self.assertIsNone(phone)
        self.assertIsNone(crisis)
        self.assertEqual(self.db.query(LongTermMemory).count(), 0)

    async def test_extracts_stable_items_and_filters_unsafe_items(self):
        user_message = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="USER",
            content="请记住，以后叫我小林。",
        )
        assistant_message = ChatMessage(
            user_id=self.user.id,
            session_id=self.session.id,
            role="ASSISTANT",
            content="好的，小林。",
        )
        self.db.add_all([user_message, assistant_message])
        self.db.flush()
        ai = FakeAi(
            f"""[
              {{"type":"PROFILE","name":"称呼","description":"学生希望被称为小林","body":"学生希望以后被称为小林。","confidence":0.9,"evidenceMessageIds":[{user_message.id}]}},
              {{"type":"SUPPORT","name":"联系方式","description":"紧急联系","body":"手机号是13800138000。","confidence":0.9,"evidenceMessageIds":[{user_message.id}]}}
            ]"""
        )
        service = LongTermMemoryService(self.db, self.settings, ai=ai)

        stored = await service.extract_from_messages(
            self.user.id,
            self.session.id,
            [
                (user_message.id, "USER", "请记住，以后叫我小林。"),
                (assistant_message.id, "ASSISTANT", "好的，小林。"),
            ],
        )
        self.db.commit()

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].name, "称呼")
        self.assertEqual(stored[0].memory_type, "PROFILE")

    async def test_relevant_selection_uses_index_ids_and_has_keyword_fallback(self):
        service = LongTermMemoryService(self.db, self.settings)
        for index in range(7):
            service.upsert(
                self.user.id,
                self.session.id,
                "CONTEXT",
                f"长期事项{index}",
                f"事项{index}的摘要",
                f"学生长期关注事项{index}。",
            )
        internship = service.upsert(
            self.user.id,
            self.session.id,
            "CONTEXT",
            "秋招计划",
            "学生正在准备秋招",
            "学生的长期目标是寻找后端开发实习。",
        )
        self.db.commit()

        selecting = LongTermMemoryService(
            self.db,
            self.settings,
            ai=FakeAi(f'["{internship.public_id}"]'),
        )
        selected = await selecting.select_relevant(self.user.id, "继续说说实习")
        self.assertEqual([item.public_id for item in selected], [internship.public_id])

        fallback = LongTermMemoryService(
            self.db,
            self.settings,
            ai=FakeAi("不是合法 JSON"),
        )
        selected = await fallback.select_relevant(self.user.id, "秋招后端实习")
        self.assertIn(internship.public_id, [item.public_id for item in selected])

    async def test_small_memory_set_does_not_inject_unrelated_items(self):
        service = LongTermMemoryService(
            self.db,
            self.settings,
            ai=FakeAi("[]"),
        )
        service.upsert(
            self.user.id,
            self.session.id,
            "PREFERENCE",
            "回复风格",
            "学生偏好简洁回复",
            "学生希望回答简洁。",
        )
        self.db.commit()

        selected = await service.select_relevant(self.user.id, "今天食堂吃什么")

        self.assertEqual(selected, [])

    async def test_disabled_user_neither_extracts_nor_reads_memory(self):
        service = LongTermMemoryService(self.db, self.settings, ai=FakeAi("[]"))
        service.upsert(
            self.user.id,
            self.session.id,
            "PREFERENCE",
            "回复风格",
            "学生偏好简洁回复",
            "学生希望回答简洁。",
        )
        self.user.long_term_memory_enabled = False
        self.db.commit()

        self.assertEqual(await service.select_relevant(self.user.id, "回复风格"), [])
        self.assertEqual(
            await service.extract_from_messages(
                self.user.id,
                self.session.id,
                [("USER", "请记住我喜欢简洁回复")],
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
