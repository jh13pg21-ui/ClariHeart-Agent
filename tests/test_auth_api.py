import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cli.users import build_parser
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import AuthSession, UserAccount


ROOT = Path(__file__).resolve().parents[1]


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.session_factory() as db:
            student = UserAccount(
                username="student",
                display_name="Demo Student",
                password_hash=hash_password("student-password"),
                password_algorithm="argon2id",
                must_reset_password=False,
            )
            student.roles = {"ROLE_USER"}
            admin = UserAccount(
                username="admin",
                display_name="Counselor Admin",
                password_hash=hash_password("admin-password"),
                password_algorithm="argon2id",
                must_reset_password=False,
            )
            admin.roles = {"ROLE_ADMIN"}
            db.add_all([student, admin])
            db.commit()

        self.settings = SimpleNamespace(
            app_environment="test",
            jwt_secret_key="test-only-secret-key-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            access_token_minutes=15,
            refresh_token_days=7,
            auth_secure_cookie=True,
            auth_cookie_samesite="lax",
            tool_queue_enabled=False,
            validate_auth_configuration=lambda: None,
        )
        self.app = create_app(self.settings)

        def override_get_db():
            with self.session_factory() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def login(self, username="student", password="student-password"):
        return self.client.post("/api/auth/login", json={"username": username, "password": password})

    def csrf_headers(self):
        return {"X-CSRF-Token": self.client.cookies.get("mindbridge_csrf")}

    def test_login_sets_secure_cookie_contract_and_authorizes_profile(self):
        response = self.login()

        self.assertEqual(response.status_code, 200)
        cookies = response.headers.get_list("set-cookie")
        access = next(item for item in cookies if item.startswith("mindbridge_access="))
        refresh = next(item for item in cookies if item.startswith("mindbridge_refresh="))
        csrf = next(item for item in cookies if item.startswith("mindbridge_csrf="))
        self.assertIn("HttpOnly", access)
        self.assertIn("HttpOnly", refresh)
        self.assertNotIn("HttpOnly", csrf)
        self.assertTrue(all("Secure" in item and "SameSite=lax" in item for item in cookies))
        self.assertEqual(self.client.get("/api/profile").json()["username"], "student")

    def test_refresh_requires_csrf_rotates_cookie_and_rejects_replay(self):
        self.login()
        old_refresh = self.client.cookies.get("mindbridge_refresh")

        self.assertEqual(self.client.post("/api/auth/refresh").status_code, 403)
        self.assertEqual(
            self.client.post("/api/auth/refresh", headers={"X-CSRF-Token": "wrong"}).status_code,
            403,
        )
        response = self.client.post("/api/auth/refresh", headers=self.csrf_headers())
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.cookies.get("mindbridge_refresh"), old_refresh)

        self.client.cookies.set("mindbridge_refresh", old_refresh, domain="testserver.local", path="/api/auth")
        replay = self.client.post("/api/auth/refresh", headers=self.csrf_headers())
        self.assertEqual(replay.status_code, 401)

    def test_logout_revokes_session_and_clears_authentication(self):
        self.login()
        with self.session_factory() as db:
            session_id = db.query(AuthSession.id).scalar()

        response = self.client.post("/api/auth/logout", headers=self.csrf_headers())

        self.assertEqual(response.status_code, 204)
        with self.session_factory() as db:
            self.assertTrue(db.get(AuthSession, session_id).revoked)
        self.assertEqual(self.client.get("/api/profile").status_code, 401)

    def test_role_protection_and_write_csrf_are_enforced(self):
        self.login()
        self.assertEqual(self.client.get("/api/admin/reports").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/chat/stream",
                json={"sessionId": None, "message": "hello"},
            ).status_code,
            403,
        )

    def test_cli_has_no_plaintext_password_argument(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["create", "alice", "plaintext-password"])

    def test_frontend_contains_no_basic_or_session_storage_credentials(self):
        sources = [
            (ROOT / "app/static/app.js").read_text(encoding="utf-8"),
            (ROOT / "app/static/student.js").read_text(encoding="utf-8"),
            (ROOT / "app/static/admin.js").read_text(encoding="utf-8"),
        ]
        for source in sources:
            self.assertNotIn("Basic ", source)
            self.assertNotIn("sessionStorage", source)
            self.assertNotIn("Authorization", source)
            self.assertIn('credentials: "same-origin"', source)


if __name__ == "__main__":
    unittest.main()
