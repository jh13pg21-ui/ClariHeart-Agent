import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import LongTermMemory, UserAccount


class LongTermMemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session() as db:
            student = self._user("student")
            other = self._user("other")
            db.add_all([student, other])
            db.flush()
            db.add_all(
                [
                    LongTermMemory(
                        public_id="own-memory",
                        user_id=student.id,
                        memory_type="PREFERENCE",
                        name="回复风格",
                        description="偏好简洁回复",
                        body="学生偏好简洁回复。",
                        content_hash="own-hash",
                    ),
                    LongTermMemory(
                        public_id="foreign-memory",
                        user_id=other.id,
                        memory_type="PROFILE",
                        name="隐私",
                        description="他人的记忆",
                        body="不应被看到。",
                        content_hash="foreign-hash",
                    ),
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
            with self.Session() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(
            self.app,
            base_url="https://testserver",
            raise_server_exceptions=False,
        )
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

    def test_list_and_delete_are_limited_to_current_user(self):
        response = self.client.get("/api/memories")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()], ["own-memory"])
        self.assertNotIn("不应被看到", response.text)

        csrf = self.client.cookies.get("mindbridge_csrf")
        foreign = self.client.delete(
            "/api/memories/foreign-memory",
            headers={"X-CSRF-Token": csrf},
        )
        own = self.client.delete(
            "/api/memories/own-memory",
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(own.status_code, 204)
        self.assertEqual(self.client.get("/api/memories").json(), [])

    def test_user_can_disable_and_purge_long_term_memory(self):
        csrf = self.client.cookies.get("mindbridge_csrf")
        response = self.client.patch(
            "/api/privacy/preferences",
            headers={"X-CSRF-Token": csrf},
            json={
                "longTermMemoryEnabled": False,
                "purgeExistingMemories": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"longTermMemoryEnabled": False, "removedMemories": 1},
        )
        self.assertEqual(self.client.get("/api/memories").json(), [])


if __name__ == "__main__":
    unittest.main()
