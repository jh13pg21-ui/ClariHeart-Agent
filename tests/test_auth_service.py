import hashlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.core.security import hash_password, verify_password
from app.models.entities import AuthSession, UserAccount
from app.services.auth import (
    AuthService,
    InvalidCredentials,
    InvalidToken,
    PasswordResetRequired,
)


def auth_settings(**overrides):
    values = {
        "app_environment": "test",
        "jwt_secret_key": "test-only-secret-key-with-at-least-32-bytes",
        "jwt_algorithm": "HS256",
        "access_token_minutes": 15,
        "refresh_token_days": 7,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.settings = auth_settings()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def add_user(self, username="student", password="student-password", **overrides):
        values = {
            "username": username,
            "display_name": username.title(),
            "password_hash": hash_password(password),
            "password_algorithm": "argon2id",
            "must_reset_password": False,
            "disabled": False,
        }
        values.update(overrides)
        user = UserAccount(**values)
        user.roles = {"ROLE_USER"}
        self.db.add(user)
        self.db.commit()
        return user

    def test_password_hash_uses_argon2id_and_verifies_only_correct_password(self):
        encoded = hash_password("correct horse battery staple")

        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong password", encoded))

    def test_legacy_password_requires_reset_without_verifying_old_hash(self):
        self.add_user(
            password_hash=hashlib.sha256(b"old-password").hexdigest(),
            password_algorithm="legacy_sha256",
            must_reset_password=True,
        )

        with patch("app.services.auth.verify_password", side_effect=AssertionError("不应验证旧哈希")):
            with self.assertRaises(PasswordResetRequired):
                AuthService(self.db, self.settings).login("student", "old-password")

    def test_login_persists_only_refresh_digest_and_emits_required_access_claims(self):
        user = self.add_user()

        tokens = AuthService(self.db, self.settings).login("student", "student-password")

        stored = self.db.get(AuthSession, int(tokens.session_id))
        self.assertEqual(stored.session_token_id, hashlib.sha256(tokens.refresh_token.encode()).hexdigest())
        self.assertNotEqual(stored.session_token_id, tokens.refresh_token)
        claims = jwt.decode(tokens.access_token, self.settings.jwt_secret_key, algorithms=["HS256"])
        self.assertEqual(claims["sub"], str(user.id))
        self.assertEqual(claims["sid"], tokens.session_id)
        self.assertEqual(claims["roles"], ["ROLE_USER"])
        self.assertTrue({"iat", "exp", "jti"} <= claims.keys())

    def test_refresh_rotates_session_and_rejects_old_token_replay(self):
        self.add_user()
        service = AuthService(self.db, self.settings)
        first = service.login("student", "student-password")

        second = service.refresh(first.refresh_token)

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertTrue(self.db.get(AuthSession, int(first.session_id)).revoked)
        with self.assertRaises(InvalidToken):
            service.refresh(first.refresh_token)

    def test_logout_revokes_session_and_disabled_user_cannot_login(self):
        self.add_user()
        service = AuthService(self.db, self.settings)
        tokens = service.login("student", "student-password")
        service.logout(tokens.session_id)
        self.assertTrue(self.db.get(AuthSession, int(tokens.session_id)).revoked)

        self.db.query(UserAccount).filter_by(username="student").update({"disabled": True})
        self.db.commit()
        with self.assertRaises(InvalidCredentials):
            service.login("student", "student-password")

    def test_expired_or_revoked_refresh_session_is_rejected(self):
        self.add_user()
        service = AuthService(self.db, self.settings)
        tokens = service.login("student", "student-password")
        session = self.db.get(AuthSession, int(tokens.session_id))
        session.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()

        with self.assertRaises(InvalidToken):
            service.refresh(tokens.refresh_token)

    def test_production_rejects_missing_or_weak_jwt_secret(self):
        for secret in ("", "short-secret"):
            with self.subTest(secret=secret):
                settings = Settings(app_environment="production", jwt_secret_key=secret)
                with self.assertRaises(ValueError):
                    settings.validate_auth_configuration()


if __name__ == "__main__":
    unittest.main()
