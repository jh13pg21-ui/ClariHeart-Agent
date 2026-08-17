import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.autonomous import AgentNodeContext, UnderstandingAgent, _context_history
from app.agents.events import AgentArtifact
from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession
from app.schemas.dtos import AiMessage
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer


class CapturingClient:
    def __init__(self, response="CHAT"):
        self.response = response
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return self.response


class EmptyPrivateMemory:
    def _key(self, agent_name, session_id): return f"{agent_name}:{session_id}"
    def load(self, agent_name, session_id): return []
    def append(self, agent_name, session_id, content): return None


def memory_context(text="是"):
    memory = AgentArtifact(
        "memory",
        "ContextAgent",
        "memory",
        {
            "history": [
                AiMessage(role="user", content="詹姆斯哈登"),
                AiMessage(role="assistant", content="他是著名篮球运动员，你想知道更多吗？"),
            ],
            "memoryBrief": "学生刚才询问詹姆斯哈登。",
            "longTermMemoryContext": "- 回复风格：先给结论。",
        },
    )
    return AgentNodeContext(turn_id="turn", session_id="session", model_input=text, artifacts=(memory,))


class ConversationMemoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_intent_classification_receives_shared_history(self):
        client = CapturingClient()
        services = SimpleNamespace(
            session=SimpleNamespace(public_id="session"),
            private_memory=EmptyPrivateMemory(),
            model_registry=SimpleNamespace(client_for=lambda name: client),
        )
        await UnderstandingAgent(services)._classify("是", memory_context())
        prompt = "\n".join(message.content for message in client.calls[0])
        self.assertIn("詹姆斯哈登", prompt)
        self.assertIn('"currentInput":"是"', prompt)

    async def test_safety_history_places_current_input_last(self):
        history = _context_history(memory_context("安慰一下"))
        self.assertEqual(history[-1].content, "安慰一下")
        self.assertEqual(history[0].content, "詹姆斯哈登")


class MysqlMemoryFallbackTests(unittest.TestCase):
    def test_mysql_history_is_used_when_redis_has_no_data(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        session = ChatSession(public_id="session", user_id=1, title="test")
        db.add(session)
        db.flush()
        db.add_all([
            ChatMessage(user_id=1, session_id=session.id, role="USER", content="我最近在准备秋招"),
            ChatMessage(user_id=1, session_id=session.id, role="ASSISTANT", content="我们可以先梳理目标岗位。"),
        ])
        db.commit()
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.settings = SimpleNamespace(redis_memory_max_messages=40)
        store.privacy = PrivacySanitizer()
        store.client = None
        history = store.load_conversation(db, session)
        self.assertEqual([item.content for item in history], ["我最近在准备秋招", "我们可以先梳理目标岗位。"])
        db.close()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
