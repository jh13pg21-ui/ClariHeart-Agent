import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import ChatSession, SecurityAuditRecord, UserAccount


class AdminReadApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.session_factory() as db:
            admin = UserAccount(
                username="admin",
                display_name="Counselor Admin",
                password_hash=hash_password("admin-password"),
                password_algorithm="argon2id",
                must_reset_password=False,
            )
            admin.roles = {"ROLE_ADMIN"}
            db.add(admin)
            db.flush()
            db.add(ChatSession(public_id="session-ok", title="Test", user_id=admin.id))
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
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(
            self.app,
            base_url="https://testserver",
            raise_server_exceptions=False,
        )
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_admin_read_endpoints_return_success_for_admin(self):
        self.assertEqual(self.client.get("/api/admin/reports").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/agent-traces").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/tool-audits").status_code, 200)

    def test_conversation_read_audits_success_not_found_and_error_without_content(self):
        self.assertEqual(self.client.get("/api/admin/conversations/session-ok").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/conversations/missing").status_code, 404)
        with patch("app.api.routes.ReportService.conversation", side_effect=RuntimeError("database broke")):
            self.assertEqual(self.client.get("/api/admin/conversations/error-session").status_code, 500)

        with self.session_factory() as db:
            records = db.query(SecurityAuditRecord).order_by(SecurityAuditRecord.id).all()
            self.assertEqual(
                [json.loads(record.details_json)["outcome"] for record in records],
                ["success", "not_found", "error"],
            )
            self.assertTrue(all(record.user_id and record.ip_address for record in records))
            serialized = "\n".join(record.details_json for record in records)
            self.assertNotIn("database broke", serialized)
            self.assertNotIn("message", serialized)
            self.assertNotIn("token", serialized.lower())


if __name__ == "__main__":
    unittest.main()
