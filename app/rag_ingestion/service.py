from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    KnowledgePage,
    KnowledgeChunk,
)
from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.repository import KnowledgeIngestionRepository
from app.rag_ingestion.schema import AccessClass
from app.services.vector_store import ChromaKnowledgeStore


SUPPORTED_UPLOADS = {
    ".pdf": {"application/pdf"},
    ".md": {"text/markdown", "text/x-markdown", "text/plain"},
    ".markdown": {"text/markdown", "text/x-markdown", "text/plain"},
    ".txt": {"text/plain"},
}


@dataclass(frozen=True)
class IngestionSubmission:
    document_id: str
    version_id: str
    job_id: str
    status: str
    created: bool
    cloud_vision_allowed: bool
    source_path: str


class KnowledgeChunkIndexer:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        vector_store=None,
    ):
        self.db = db
        self.settings = settings
        self.vector_store = vector_store or ChromaKnowledgeStore(settings)

    def index(self, chunks: list[KnowledgeChunk]) -> set[str]:
        children = [
            chunk
            for chunk in chunks
            if chunk.chunk_kind == "CHILD" and chunk.stable_id and chunk.content.strip()
        ]
        stable_ids = {chunk.stable_id for chunk in children if chunk.stable_id}
        if not children:
            return stable_ids
        if not self.vector_store.can_embed:
            if self.settings.knowledge_vector_required:
                raise RuntimeError(
                    getattr(self.vector_store, "error", "") or "The vector store is unavailable."
                )
            return stable_ids
        embeddings = self.vector_store.embed_texts([chunk.content for chunk in children])
        if len(embeddings) != len(children):
            raise ValueError("Embedding response count did not match child chunks.")
        for chunk, embedding in zip(children, embeddings):
            chunk.embedding_json = json.dumps(embedding, separators=(",", ":"))
            chunk.embedding_model = self.settings.openai_embedding_model
            chunk.embedding_dimension = len(embedding)
        indexed = self.vector_store.upsert_chunks(children, embeddings)
        if indexed != len(children):
            raise ValueError("The vector index did not acknowledge every child chunk.")
        self.db.commit()
        return stable_ids


def dispatch_ingestion_task(job_id: str) -> None:
    from app.workers.celery_app import celery_app

    celery_app.send_task("app.workers.ingestion_tasks.ingest_knowledge_document", args=[job_id])


class KnowledgeIngestionService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        *,
        task_dispatcher: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.settings = settings
        artifact_root = Path(settings.rag_artifact_dir)
        if not artifact_root.is_absolute():
            artifact_root = settings.project_root / artifact_root
        self.artifacts = ArtifactStore(artifact_root)
        self.repository = KnowledgeIngestionRepository(db)
        self.task_dispatcher = task_dispatcher or dispatch_ingestion_task

    def submit_file(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
        actor: str,
        access_class: AccessClass = AccessClass.ADMIN_PRIVATE,
        cloud_vision_allowed: bool = False,
        source_key: str | None = None,
    ) -> IngestionSubmission:
        display_name, canonical_mime = self._validate_file(filename, mime_type, data)
        sha256 = hashlib.sha256(data).hexdigest()
        submission = self.repository.ensure_submission(
            source_key=source_key or f"admin:{display_name.casefold()}",
            display_name=display_name,
            mime_type=canonical_mime,
            sha256=sha256,
            size_bytes=len(data),
            access_class=access_class,
            cloud_vision_allowed=bool(cloud_vision_allowed),
            trigger_actor=actor,
            pipeline_fingerprint=self.settings.rag_pipeline_fingerprint,
        )
        source_path = self.artifacts.write_source(submission.document.id, sha256, data)
        if submission.created:
            self.task_dispatcher(submission.job.id)
        return self._submission_result(submission, source_path)

    def retry(
        self,
        job_id: str,
        *,
        actor: str,
        cloud_vision_allowed: bool | None = None,
    ) -> IngestionSubmission:
        job = self.repository.create_retry_job(
            job_id,
            trigger_actor=actor,
            cloud_vision_allowed=cloud_vision_allowed,
        )
        _, version, document = self.repository.get_context(job.id)
        source_path = self.artifacts.version_dir(document.id, version.sha256) / "source.bin"
        if not source_path.is_file():
            raise FileNotFoundError("The source artifact for this ingestion version is missing.")
        self.task_dispatcher(job.id)
        return IngestionSubmission(
            document_id=document.id,
            version_id=version.id,
            job_id=job.id,
            status=job.status,
            created=True,
            cloud_vision_allowed=document.cloud_vision_allowed,
            source_path=str(source_path),
        )

    def get_job(self, job_id: str) -> dict:
        job, version, document = self.repository.get_context(job_id)
        return self._job_dict(job, version, document)

    def list_documents(self) -> list[dict]:
        rows = self.db.query(KnowledgeDocument).order_by(KnowledgeDocument.updated_at.desc()).all()
        return [self._document_dict(row) for row in rows]

    def get_document(self, document_id: str) -> dict:
        document = self.db.get(KnowledgeDocument, document_id)
        if document is None:
            raise LookupError("Knowledge document not found.")
        versions = (
            self.db.query(KnowledgeDocumentVersion)
            .filter_by(document_id=document_id)
            .order_by(KnowledgeDocumentVersion.created_at.desc())
            .all()
        )
        result = self._document_dict(document)
        result["versions"] = [
            {
                "versionId": row.id,
                "sha256": row.sha256,
                "status": row.status,
                "pageCount": row.page_count,
                "createdAt": row.created_at,
                "completedAt": row.completed_at,
            }
            for row in versions
        ]
        return result

    def list_pages(self, document_id: str) -> list[dict]:
        document = self.db.get(KnowledgeDocument, document_id)
        if document is None:
            raise LookupError("Knowledge document not found.")
        if not document.active_version_id:
            return []
        rows = (
            self.db.query(KnowledgePage)
            .filter_by(document_version_id=document.active_version_id)
            .order_by(KnowledgePage.page_number.asc())
            .all()
        )
        return [
            {
                "pageNumber": row.page_number,
                "textStrategy": row.text_strategy,
                "structureStrategy": row.structure_strategy,
                "status": row.status,
                "quality": row.quality_json,
            }
            for row in rows
        ]

    def _validate_file(self, filename: str, mime_type: str, data: bytes) -> tuple[str, str]:
        display_name = Path((filename or "").replace("\\", "/")).name.strip()
        suffix = Path(display_name).suffix.casefold()
        if not display_name or suffix not in SUPPORTED_UPLOADS:
            raise ValueError("Only PDF, Markdown, and TXT knowledge files are supported.")
        normalized_mime = (mime_type or "").split(";", 1)[0].strip().casefold()
        if normalized_mime not in SUPPORTED_UPLOADS[suffix]:
            raise ValueError("The upload MIME type does not match its file extension.")
        if not data:
            raise ValueError("The uploaded file is empty.")
        if len(data) > self.settings.rag_ingestion_max_file_size_bytes:
            raise ValueError("The uploaded file exceeds the configured size limit.")
        if suffix == ".pdf" and not data.startswith(b"%PDF-"):
            raise ValueError("The uploaded file is not a valid PDF stream.")
        canonical_mime = "application/pdf" if suffix == ".pdf" else (
            "text/markdown" if suffix in {".md", ".markdown"} else "text/plain"
        )
        return display_name, canonical_mime

    @staticmethod
    def _submission_result(submission, source_path: Path) -> IngestionSubmission:
        return IngestionSubmission(
            document_id=submission.document.id,
            version_id=submission.version.id,
            job_id=submission.job.id,
            status=submission.job.status,
            created=submission.created,
            cloud_vision_allowed=submission.document.cloud_vision_allowed,
            source_path=str(source_path),
        )

    @staticmethod
    def _document_dict(document: KnowledgeDocument) -> dict:
        return {
            "documentId": document.id,
            "sourceKey": document.source_key,
            "displayName": document.display_name,
            "mimeType": document.mime_type,
            "accessClass": document.access_class,
            "cloudVisionAllowed": document.cloud_vision_allowed,
            "activeVersionId": document.active_version_id,
            "updatedAt": document.updated_at,
        }

    @staticmethod
    def _job_dict(job, version, document) -> dict:
        return {
            "jobId": job.id,
            "documentId": document.id,
            "versionId": version.id,
            "displayName": document.display_name,
            "stage": job.stage,
            "status": job.status,
            "progressPage": job.progress_page,
            "totalPages": job.total_pages,
            "attempts": job.attempts,
            "error": (
                {
                    "code": job.error_code,
                    "message": job.error_message,
                    "retryable": job.error_retryable,
                }
                if job.error_code
                else None
            ),
            "createdAt": job.created_at,
            "updatedAt": job.updated_at,
        }
