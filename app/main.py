from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.core.bootstrap import ensure_database_current
from app.core.config import get_settings
from app.core.security import csrf_is_valid


def create_app(settings=None) -> FastAPI:
    runtime_settings = settings or get_settings()
    app = FastAPI(title="MindBridge Python", version="0.1.0")
    app.state.settings = runtime_settings

    @app.middleware("http")
    async def security_and_frontend_headers(request: Request, call_next):
        if (
            request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path != "/api/auth/login"
            and not csrf_is_valid(request)
        ):
            return JSONResponse({"detail": "CSRF 校验失败"}, status_code=403)
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.on_event("startup")
    def startup() -> None:
        runtime_settings.validate_auth_configuration()
        ensure_database_current()

    app.include_router(auth_router)
    app.include_router(router)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
