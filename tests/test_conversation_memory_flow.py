import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.autonomous import (
    ResponseAgent,
    SafetyAgent,
    UnderstandingAgent,
    _context_history,
)
from app.agents.coordinator import EventDrivenCoordinator
from app.agents.events import AgentArtifact, AgentTask, CollaborationBlackboard
from app.agents.registry import AgentRegistry
from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession
from app.schemas.dtos import AiMessage
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer


class CapturingClient:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[list[AiMessage]] = []

    async def complete(self, messages: list[AiMessage], **kwargs) -> str:
        self.calls.append(messages)
        return self.response


class EmptyPrivateMemory:
    def _key(self, agent_name: str, session_id: str) -> str:
        return f"{agent_name}:{session_id}"

    def load(self, agent_name: str, session_id: str) -> list[AiMessage]:
        return []

    def append(self, agent_name: str, session_id: str, content: str) -> None:
        return None


def memory_board(current_input: str = "是") -> CollaborationBlackboard:
    return (
        CollaborationBlackboard(
            turn_id="turn",
            user_input=current_input,
            model_input=current_input,
        )
        .add_artifact(
            AgentArtifact(
                id="memory",
                owner="ContextAgent",
                kind="memory",
                payload={
                    "history": [
                        AiMessage(role="user", content="詹姆斯哈登"),
                        AiMessage(
                            role="assistant",
                            content="他是著名篮球运动员，你想知道更多吗？",
                        ),
                    ],
                    "memoryBrief": "学生刚才询问詹姆斯哈登并希望继续了解。",
                    "longTermMemoryContext": (
                        "- [PREFERENCE] 回复风格：学生希望先给结论，再给简短说明。"
                    ),
                },
            )
        )
    )


def agent_services(client: CapturingClient) -> SimpleNamespace:
    profile = SimpleNamespace(provider="mock", model="memory-test-model")
    return SimpleNamespace(
        settings=SimpleNamespace(),
        user=SimpleNamespace(display_name="同学"),
        session=SimpleNamespace(public_id="session"),
        private_memory=EmptyPrivateMemory(),
        model_registry=SimpleNamespace(
            client_for=lambda name: client,
            profile_for=lambda name: profile,
        ),
    )


class ConversationMemoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_agents_do_not_claim_follow_up_analysis_before_memory_is_ready(self):
        board = CollaborationBlackboard(
            turn_id="turn",
            user_input="继续",
            model_input="继续",
        )
        root = AgentTask(
            id="task:root",
            title="Resolve user turn",
            metadata={"kind": "root"},
        )
        client = CapturingClient("CHAT")

        understanding = UnderstandingAgent(agent_services(client)).decide(root, board)
        safety = SafetyAgent(agent_services(client)).decide(root, board)

        self.assertFalse(understanding.claim)
        self.assertFalse(safety.claim)

    async def test_intent_classification_receives_shared_conversation_history(self):
        client = CapturingClient("CHAT")
        agent = UnderstandingAgent(agent_services(client))

        await agent._classify("是", memory_board("是"))

        prompt = "\n".join(message.content for message in client.calls[0])
        self.assertIn("詹姆斯哈登", prompt)
        self.assertIn("当前输入：\n是", prompt)

    async def test_safety_history_contains_memory_before_current_follow_up(self):
        history = _context_history(memory_board("安慰一下"))

        self.assertEqual(
            [(message.role, message.content) for message in history],
            [
                ("user", "詹姆斯哈登"),
                ("assistant", "他是著名篮球运动员，你想知道更多吗？"),
                ("user", "安慰一下"),
            ],
        )

    async def test_normal_chat_response_uses_memory_without_full_context_artifact(self):
        client = CapturingClient("当然，哈登目前……")
        board = (
            memory_board("是")
            .add_artifact(
                AgentArtifact(
                    id="intent",
                    owner="UnderstandingAgent",
                    kind="intent",
                    payload={"intent": "CHAT"},
                )
            )
            .add_artifact(
                AgentArtifact(
                    id="risk",
                    owner="SafetyAgent",
                    kind="risk",
                    payload={"risk": "LOW"},
                )
            )
        )

        await ResponseAgent(agent_services(client)).act(
            AgentTask(id="response", title="response"),
            board,
        )

        prompt = "\n".join(message.content for message in client.calls[0])
        self.assertIn("詹姆斯哈登", prompt)
        self.assertIn("学生希望先给结论", prompt)
        self.assertEqual(client.calls[0][-1].content, "是")

    async def test_coordinator_waits_for_memory_before_intent_and_safety_tasks(self):
        settings = SimpleNamespace(
            agent_max_rounds=8,
            agent_max_claims_per_round=4,
            agent_max_claims_per_agent=3,
            agent_final_acceptance_min_confidence=0.6,
        )
        coordinator_agent = SimpleNamespace(name="CoordinatorAgent")
        coordinator = EventDrivenCoordinator(
            AgentRegistry([]),
            coordinator_agent,
            settings,
        )
        board = CollaborationBlackboard(
            turn_id="turn",
            user_input="继续",
            model_input="继续",
        )

        before_memory = coordinator._derive_missing_work(board)

        self.assertIn("task:prefetch-memory", before_memory.tasks)
        self.assertNotIn("task:understand", before_memory.tasks)
        self.assertNotIn("task:assess-safety", before_memory.tasks)

        after_memory = coordinator._derive_missing_work(
            before_memory.add_artifact(
                AgentArtifact(
                    id="memory",
                    owner="ContextAgent",
                    kind="memory",
                    payload={"history": [], "memoryBrief": "无相关历史记忆。"},
                )
            )
        )

        self.assertIn("task:understand", after_memory.tasks)
        self.assertIn("task:assess-safety", after_memory.tasks)


class MysqlMemoryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_mysql_history_is_used_when_redis_has_no_data(self):
        db = self.session_factory()
        session = ChatSession(public_id="session", user_id=1, title="test")
        db.add(session)
        db.flush()
        db.add_all(
            [
                ChatMessage(
                    user_id=1,
                    session_id=session.id,
                    role="USER",
                    content="我最近在准备秋招",
                ),
                ChatMessage(
                    user_id=1,
                    session_id=session.id,
                    role="ASSISTANT",
                    content="我们可以先梳理目标岗位。",
                ),
            ]
        )
        db.commit()

        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.settings = SimpleNamespace(redis_memory_max_messages=40)
        store.privacy = PrivacySanitizer()
        store.client = None
        load_conversation = getattr(
            store,
            "load_conversation",
            lambda database, chat_session: [],
        )

        history = load_conversation(db, session)

        self.assertEqual(
            [message.content for message in history],
            ["我最近在准备秋招", "我们可以先梳理目标岗位。"],
        )
        db.close()


if __name__ == "__main__":
    unittest.main()
