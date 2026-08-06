from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.bootstrap import seed_data, submit_builtin_knowledge
from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeDocument, KnowledgeIngestionJob, UserAccount


ROOT = Path(__file__).resolve().parents[2]


def test_web_and_ingestion_images_keep_paddle_isolated():
    web = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ingestion = (ROOT / "Dockerfile.ingestion").read_text(encoding="utf-8")
    assert "requirements-ingestion.txt" not in web
    assert "requirements-ingestion.txt" in ingestion
    assert "--queues=mindbridge.ingestion" in (ROOT / "docker-compose.yml").read_text(
        encoding="utf-8"
    )


def test_seed_data_only_seeds_accounts_and_builtin_sync_is_async(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(
            app_environment="test",
            rag_artifact_dir=str(tmp_path / "artifacts"),
            knowledge_vector_enabled=False,
        )
        knowledge = tmp_path / "knowledge"
        knowledge.mkdir()
        (knowledge / "guide.md").write_text("# Guide\n\nGrounding steps", encoding="utf-8")
        pdf_dir = knowledge / "pdf"
        pdf_dir.mkdir()
        (pdf_dir / "guide.pdf").write_bytes(b"%PDF-1.4\nfixture")
        calls = []

        seed_data(db, settings=settings)
        assert db.query(UserAccount).count() == 2
        assert db.query(KnowledgeDocument).count() == 0

        submitted = submit_builtin_knowledge(
            db,
            settings=settings,
            knowledge_root=knowledge,
            task_dispatcher=lambda job_id: calls.append(job_id),
        )
        assert len(submitted) == 2
        assert db.query(KnowledgeDocument).count() == 2
        assert db.query(KnowledgeIngestionJob).count() == 2
        assert calls == [item.job_id for item in submitted]
    engine.dispose()
