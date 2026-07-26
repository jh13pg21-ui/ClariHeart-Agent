from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import ACCESS_COOKIE, CSRF_COOKIE, REFRESH_COOKIE, current_user
from app.models.entities import UserAccount
from app.services.auth import (
    AuthenticationError,
    AuthService,
    AuthTokens,
    InvalidToken,
    PasswordResetRequired,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


def _set_auth_cookies(response: Response, tokens: AuthTokens, settings) -> None:
    common = {
        "secure": settings.auth_secure_cookie,
        "samesite": settings.auth_cookie_samesite,
    }
    response.set_cookie(
        ACCESS_COOKIE,
        tokens.access_token,
        httponly=True,
        max_age=settings.access_token_minutes * 60,
        path="/",
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        httponly=True,
        max_age=settings.refresh_token_days * 86400,
        path="/api/auth",
        **common,
    )
    response.set_cookie(
        CSRF_COOKIE,
        tokens.csrf_token,
        httponly=False,
        max_age=settings.refresh_token_days * 86400,
        path="/",
        **common,
    )


def _profile(user: UserAccount) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [{"authority": role} for role in user.roles],
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Annotated[Session, Depends(get_db)]):
    try:
        tokens = AuthService(db, request.app.state.settings).login(payload.username, payload.password)
    except PasswordResetRequired as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuthenticationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    user = db.get(UserAccount, int(AuthService(db, request.app.state.settings).decode_access_token(tokens.access_token)["sub"]))
    _set_auth_cookies(response, tokens, request.app.state.settings)
    return _profile(user)


@router.post("/refresh")
def refresh(request: Request, response: Response, db: Annotated[Session, Depends(get_db)]):
    refresh_token = request.cookies.get(REFRESH_COOKIE, "")
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Refresh Cookie")
    try:
        tokens = AuthService(db, request.app.state.settings).refresh(refresh_token)
    except AuthenticationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    _set_auth_cookies(response, tokens, request.app.state.settings)
    return {"status": "refreshed"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    _: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    service = AuthService(db, request.app.state.settings)
    try:
        claims = service.decode_access_token(request.cookies.get(ACCESS_COOKIE, ""))
        service.logout(claims["sid"])
    except InvalidToken:
        pass
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return None
