import hmac
from datetime import datetime
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import AuthSession, UserAccount


ACCESS_COOKIE = "mindbridge_access"
REFRESH_COOKIE = "mindbridge_refresh"
CSRF_COOKIE = "mindbridge_csrf"
_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _password_hasher.verify(hashed, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def authenticate_request(request: Request, db: Session) -> UserAccount:
    from app.services.auth import AuthService, InvalidToken

    access_token = request.cookies.get(ACCESS_COOKIE, "")
    if not access_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证 Cookie")
    settings = request.app.state.settings
    try:
        claims = AuthService(db, settings).decode_access_token(access_token)
        session_id = int(claims["sid"])
        user_id = int(claims["sub"])
    except (InvalidToken, TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "认证信息无效") from exc

    auth_session = db.get(AuthSession, session_id)
    if (
        auth_session is None
        or auth_session.user_id != user_id
        or auth_session.revoked
        or auth_session.expires_at <= datetime.utcnow()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session 无效或已过期")
    user = db.get(UserAccount, user_id)
    if user is None or user.disabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    if sorted(claims.get("roles", [])) != sorted(user.roles):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "角色声明已失效")
    return user


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    return authenticate_request(request, db)


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


def csrf_is_valid(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("X-CSRF-Token", "")
    return bool(cookie and header and hmac.compare_digest(cookie, header))
