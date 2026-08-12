from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.rag_ingestion.schema import AccessClass
from app.rag_ingestion.service import KnowledgeIngestionService
from app.services.knowledge import KnowledgeService
from app.workers.ingestion_tasks import run_ingestion_job


def test_text_submission_pipeline_activation_and_retrieval_end_to_end(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(
            app_environment="test",
            rag_artifact_dir=str(tmp_path / "artifacts"),
            knowledge_vector_enabled=False,
        )
        submitted = KnowledgeIngestionService(
            db,
            settings,
            task_dispatcher=lambda _: None,
        ).submit_file(
            filename="grounding.md",
            data=(
                "# Grounding\n\n## Five senses\n\n"
                "Name five things you can see and four things you can feel."
            ).encode(),
            mime_type="text/markdown",
            actor="bootstrap",
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
            source_key="builtin:grounding.md",
        )

        run_ingestion_job(db, settings, submitted.job_id)
        job = KnowledgeIngestionService(db, settings).get_job(submitted.job_id)
        assert job["status"] == "COMPLETED"
        result = KnowledgeService(db, settings).retrieve("five things you can see", top_k=1)[0]
        assert result.document_id == submitted.document_id
        assert result.page_start == 1
        assert result.block_ids
        assert "Five senses" in result.content
    engine.dispose()
