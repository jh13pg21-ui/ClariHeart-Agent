from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import create_app
from app.models.entities import UserAccount


def test_admin_upload_returns_202_and_job_can_be_read(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with session_factory() as db:
        admin = UserAccount(
            username="admin",
            display_name="Admin",
            password_hash=hash_password("admin-password"),
            password_algorithm="argon2id",
            must_reset_password=False,
        )
        admin.roles = {"ROLE_ADMIN"}
        db.add(admin)
        db.commit()

    settings = Settings(
        app_environment="test",
        jwt_secret_key="test-only-secret-key-with-at-least-32-bytes",
        database_url="sqlite://",
        rag_artifact_dir=str(tmp_path / "artifacts"),
        knowledge_vector_enabled=False,
    )
    app = create_app(settings)

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with patch("app.main.ensure_database_current"), TestClient(
        app, base_url="https://testserver"
    ) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get("mindbridge_csrf")
        with patch("app.rag_ingestion.service.dispatch_ingestion_task") as dispatch:
            response = client.post(
                "/api/admin/knowledge/files",
                headers={"X-CSRF-Token": csrf},
                files={"file": ("guide.pdf", b"%PDF-1.4\nfixture", "application/pdf")},
            )
        assert response.status_code == 202
        body = response.json()
        assert {"documentId", "versionId", "jobId", "status"} <= set(body)
        dispatch.assert_called_once_with(body["jobId"])

        job = client.get(f"/api/admin/knowledge/jobs/{body['jobId']}")
        assert job.status_code == 200
        assert job.json()["status"] == "PENDING"
    engine.dispose()
