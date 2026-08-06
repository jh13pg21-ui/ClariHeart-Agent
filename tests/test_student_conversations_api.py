import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import ChatMessage, ChatSession, UserAccount


class StudentConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.session_factory() as db:
            student = self._user("student")
            other = self._user("other")
            db.add_all([student, other])
            db.flush()
            older = self._session(db, student, "student-old", "旧会话", datetime(2026, 7, 28, 8, 0))
            newer = self._session(db, student, "student-new", "新会话", datetime(2026, 7, 29, 8, 0))
            foreign = self._session(db, other, "other-session", "他人会话", datetime(2026, 7, 29, 9, 0))
            db.add_all(
                [
                    ChatMessage(user_id=student.id, session_id=older.id, role="USER", content="旧问题"),
                    ChatMessage(user_id=student.id, session_id=newer.id, role="USER", content="新问题"),
                    ChatMessage(user_id=student.id, session_id=newer.id, role="ASSISTANT", content="新回答"),
                    ChatMessage(user_id=other.id, session_id=foreign.id, role="USER", content="隐私内容"),
                ]
            )
            db.commit()

        settings = SimpleNamespace(
            app_environment="test",
            jwt_secret_key="test-only-secret-key-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            access_token_minutes=15,
            refresh_token_days=7,
            auth_secure_cookie=True,
            auth_cookie_samesite="lax",
            validate_auth_configuration=lambda: None,
        )
        self.app = create_app(settings)

        def override_get_db():
            with self.session_factory() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False)
        response = self.client.post(
            "/api/auth/login",
            json={"username": "student", "password": "student-password"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    @staticmethod
    def _user(username):
        user = UserAccount(
            username=username,
            display_name=username,
            password_hash=hash_password("student-password"),
            password_algorithm="argon2id",
            must_reset_password=False,
        )
        user.roles = {"ROLE_USER"}
        return user

    @staticmethod
    def _session(db, user, public_id, title, updated_at):
        session = ChatSession(
            public_id=public_id,
            title=title,
            user_id=user.id,
            created_at=updated_at - timedelta(hours=1),
            updated_at=updated_at,
        )
        db.add(session)
        db.flush()
        return session

    def test_list_returns_only_current_students_sessions_newest_first(self):
        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual([item["sessionId"] for item in payload], ["student-new", "student-old"])
        self.assertEqual(payload[0]["lastMessage"], "新回答")
        self.assertTrue(payload[0]["updatedAt"].endswith("Z"))
        self.assertNotIn("other-session", response.text)
        self.assertNotIn("隐私内容", response.text)

    def test_detail_returns_messages_and_hides_other_students_session(self):
        own = self.client.get("/api/conversations/student-new")
        foreign = self.client.get("/api/conversations/other-session")

        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual([item["content"] for item in own.json()["messages"]], ["新问题", "新回答"])
        self.assertTrue(all(item["createdAt"].endswith("Z") for item in own.json()["messages"]))
        self.assertEqual(foreign.status_code, 404)
        self.assertNotIn("隐私内容", foreign.text)


if __name__ == "__main__":
    unittest.main()
