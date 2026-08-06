from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.chunking import ChunkingConfig, StructureAwareChunker
from app.rag_ingestion.errors import RetryableIngestionError
from app.rag_ingestion.fusion import PageEvidenceFusion
from app.rag_ingestion.ocr.paddle import PaddleOcrProvider
from app.rag_ingestion.parsers.liteparse import LiteParseDocumentParser
from app.rag_ingestion.parsers.text import TextDocumentParser
from app.rag_ingestion.pipeline import IngestionPipeline
from app.rag_ingestion.repository import KnowledgeIngestionRepository
from app.rag_ingestion.routing import PageRouter
from app.rag_ingestion.service import KnowledgeChunkIndexer
from app.rag_ingestion.vision.openai_compatible import OpenAICompatibleVisionProvider
from app.workers.celery_app import celery_app


worker_settings = get_settings()


def build_ingestion_pipeline(db: Session, settings: Settings) -> IngestionPipeline:
    artifact_path = Path(settings.rag_artifact_dir)
    artifact_store = ArtifactStore(
        artifact_path if artifact_path.is_absolute() else settings.project_root / artifact_path
    )
    text_parser = TextDocumentParser()
    parsers = {
        "application/pdf": LiteParseDocumentParser(
            artifact_store,
            dpi=settings.rag_page_render_dpi,
        ),
        "text/plain": text_parser,
        "text/markdown": text_parser,
    }
    if settings.rag_ocr_provider != "paddleocr":
        raise ValueError(f"Unsupported OCR provider: {settings.rag_ocr_provider}")
    if settings.rag_vision_provider != "openai_compatible":
        raise ValueError(f"Unsupported Vision provider: {settings.rag_vision_provider}")
    return IngestionPipeline(
        repository=KnowledgeIngestionRepository(db),
        artifact_store=artifact_store,
        parsers=parsers,
        router=PageRouter(),
        ocr_provider=PaddleOcrProvider(device=settings.rag_ocr_device),
        vision_provider=OpenAICompatibleVisionProvider(
            base_url=settings.effective_rag_vision_base_url,
            api_key=settings.effective_rag_vision_api_key,
            model=settings.rag_vision_model,
            detail=settings.rag_vision_detail,
            timeout_seconds=settings.rag_vision_timeout_seconds,
            max_attempts=settings.rag_vision_max_attempts,
        ),
        fusion=PageEvidenceFusion(),
        chunker=StructureAwareChunker(
            ChunkingConfig(
                child_target_tokens=settings.rag_child_target_tokens,
                child_min_tokens=settings.rag_child_min_tokens,
                child_max_tokens=settings.rag_child_max_tokens,
                child_overlap_tokens=settings.rag_child_overlap_tokens,
                parent_target_tokens=settings.rag_parent_target_tokens,
                parent_max_tokens=settings.rag_parent_max_tokens,
            )
        ),
        indexer=KnowledgeChunkIndexer(db, settings),
    )


def run_ingestion_job(db: Session, settings: Settings, job_id: str) -> None:
    build_ingestion_pipeline(db, settings).run(job_id)


@celery_app.task(
    bind=True,
    name="app.workers.ingestion_tasks.ingest_knowledge_document",
    max_retries=worker_settings.rag_ingestion_max_attempts,
)
def ingest_knowledge_document(self, job_id: str) -> dict:
    db = SessionLocal()
    try:
        run_ingestion_job(db, worker_settings, job_id)
    except RetryableIngestionError as exc:
        countdown = min(300, 15 * (2 ** self.request.retries))
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        db.close()
    return {"jobId": job_id, "status": "COMPLETED"}
