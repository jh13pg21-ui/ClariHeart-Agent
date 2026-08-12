from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.entities import AuthSession, UserAccount


class AuthenticationError(Exception):
    pass


class InvalidCredentials(AuthenticationError):
    pass


class PasswordResetRequired(AuthenticationError):
    pass


class InvalidToken(AuthenticationError):
    pass


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    csrf_token: str
    session_id: str


class AuthService:
    def __init__(self, db: Session, settings):
        self.db = db
        self.settings = settings

    def login(self, username: str, password: str) -> AuthTokens:
        user = self.db.query(UserAccount).filter(UserAccount.username == username).first()
        if user is None or user.disabled:
            raise InvalidCredentials("用户名或密码错误")
        if user.password_algorithm == "legacy_sha256" or user.must_reset_password:
            raise PasswordResetRequired("该账号必须先安全重置密码")
        if user.password_algorithm != "argon2id" or not verify_password(password, user.password_hash):
            raise InvalidCredentials("用户名或密码错误")
        return self._create_session(user)

    def refresh(self, refresh_token: str) -> AuthTokens:
        token_digest = self._refresh_digest(refresh_token)
        old_session = (
            self.db.query(AuthSession)
            .filter(AuthSession.session_token_id == token_digest)
            .with_for_update()
            .first()
        )
        now = datetime.utcnow()
        if old_session is None or old_session.revoked or old_session.expires_at <= now:
            raise InvalidToken("Refresh Token 无效或已过期")
        user = self.db.get(UserAccount, old_session.user_id)
        if user is None or user.disabled:
            raise InvalidToken("用户不存在或已禁用")

        try:
            old_session.revoked = True
            old_session.revoked_at = now
            tokens = self._create_session(user, commit=False)
            self.db.commit()
            return tokens
        except Exception:
            self.db.rollback()
            raise

    def logout(self, session_id: str) -> None:
        try:
            database_id = int(session_id)
        except (TypeError, ValueError) as exc:
            raise InvalidToken("Session 标识无效") from exc
        auth_session = self.db.get(AuthSession, database_id)
        if auth_session is not None and not auth_session.revoked:
            auth_session.revoked = True
            auth_session.revoked_at = datetime.utcnow()
            self.db.commit()

    def decode_access_token(self, access_token: str) -> dict:
        try:
            return jwt.decode(
                access_token,
                self.settings.jwt_secret_key,
                algorithms=[self.settings.jwt_algorithm],
                options={"require": ["sub", "sid", "roles", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken("Access Token 无效或已过期") from exc

    def _create_session(self, user: UserAccount, *, commit: bool = True) -> AuthTokens:
        now = datetime.utcnow()
        refresh_token = secrets.token_urlsafe(32)
        auth_session = AuthSession(
            user_id=user.id,
            session_token_id=self._refresh_digest(refresh_token),
            issued_at=now,
            expires_at=now + timedelta(days=self.settings.refresh_token_days),
            revoked=False,
        )
        self.db.add(auth_session)
        self.db.flush()
        access_token = self._encode_access_token(user, auth_session.id, now)
        tokens = AuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            csrf_token=secrets.token_urlsafe(32),
            session_id=str(auth_session.id),
        )
        if commit:
            self.db.commit()
        return tokens

    def _encode_access_token(self, user: UserAccount, session_id: int, issued_at: datetime) -> str:
        issued_utc = issued_at.replace(tzinfo=timezone.utc)
        payload = {
            "sub": str(user.id),
            "sid": str(session_id),
            "roles": user.roles,
            "iat": issued_utc,
            "exp": issued_utc + timedelta(minutes=self.settings.access_token_minutes),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)

    @staticmethod
    def _refresh_digest(refresh_token: str) -> str:
        return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
