from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.entities import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    KnowledgePage,
)
from app.rag_ingestion.ids import stable_document_id, stable_version_id
from app.rag_ingestion.schema import AccessClass, CanonicalPage, ChunkDraft, ChunkKind


ACTIVE_JOB_STATUSES = {"PENDING", "RUNNING"}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Submission:
    document: KnowledgeDocument
    version: KnowledgeDocumentVersion
    job: KnowledgeIngestionJob
    created: bool


class KnowledgeIngestionRepository:
    def __init__(self, db: Session):
        self.db = db

    def ensure_submission(
        self,
        *,
        source_key: str,
        display_name: str,
        mime_type: str,
        sha256: str,
        size_bytes: int,
        access_class: AccessClass,
        cloud_vision_allowed: bool,
        trigger_actor: str,
        pipeline_fingerprint: str,
    ) -> Submission:
        document = self.db.query(KnowledgeDocument).filter_by(source_key=source_key).one_or_none()
        if document is None:
            document = KnowledgeDocument(
                id=stable_document_id(source_key),
                source_key=source_key,
                display_name=display_name,
                mime_type=mime_type,
                access_class=access_class.value,
                cloud_vision_allowed=cloud_vision_allowed,
            )
            self.db.add(document)
            self.db.flush()
        else:
            document.display_name = display_name
            document.mime_type = mime_type
            document.access_class = access_class.value
            document.cloud_vision_allowed = cloud_vision_allowed
            document.updated_at = _now()

        version = (
            self.db.query(KnowledgeDocumentVersion)
            .filter_by(document_id=document.id, sha256=sha256)
            .one_or_none()
        )
        created = version is None
        if version is None:
            version = KnowledgeDocumentVersion(
                id=stable_version_id(document.id, sha256, pipeline_fingerprint),
                document_id=document.id,
                sha256=sha256,
                size_bytes=size_bytes,
                status="PENDING",
                pipeline_fingerprint=pipeline_fingerprint,
                previous_version_id=document.active_version_id,
            )
            self.db.add(version)
            self.db.flush()

        job = (
            self.db.query(KnowledgeIngestionJob)
            .filter(KnowledgeIngestionJob.document_version_id == version.id)
            .filter(KnowledgeIngestionJob.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(KnowledgeIngestionJob.created_at.desc())
            .first()
        )
        if job is None and not created:
            job = (
                self.db.query(KnowledgeIngestionJob)
                .filter_by(document_version_id=version.id)
                .order_by(KnowledgeIngestionJob.created_at.desc())
                .first()
            )
        if job is None:
            job = KnowledgeIngestionJob(
                id=f"job_{uuid.uuid4().hex[:24]}",
                document_version_id=version.id,
                stage="PENDING",
                status="PENDING",
                trigger_actor=trigger_actor,
            )
            self.db.add(job)
        self.db.commit()
        return Submission(document=document, version=version, job=job, created=created)

    def get_context(self, job_id: str) -> tuple[KnowledgeIngestionJob, KnowledgeDocumentVersion, KnowledgeDocument]:
        job = self.db.get(KnowledgeIngestionJob, job_id)
        if job is None:
            raise LookupError("摄取任务不存在")
        version = self.db.get(KnowledgeDocumentVersion, job.document_version_id)
        if version is None:
            raise LookupError("文档版本不存在")
        document = self.db.get(KnowledgeDocument, version.document_id)
        if document is None:
            raise LookupError("逻辑文档不存在")
        return job, version, document

    def create_retry_job(
        self,
        job_id: str,
        *,
        trigger_actor: str,
        cloud_vision_allowed: bool | None = None,
    ) -> KnowledgeIngestionJob:
        previous, version, document = self.get_context(job_id)
        if previous.status == "FAILED" and not previous.error_retryable:
            raise ValueError("The failed ingestion job is not retryable.")
        if previous.status == "NEEDS_REVIEW":
            if cloud_vision_allowed is not True:
                raise ValueError("Review jobs require explicit cloud Vision authorization.")
        elif previous.status != "FAILED":
            raise ValueError("Only FAILED or NEEDS_REVIEW jobs can be retried.")

        active = (
            self.db.query(KnowledgeIngestionJob)
            .filter_by(document_version_id=version.id)
            .filter(KnowledgeIngestionJob.status.in_(ACTIVE_JOB_STATUSES))
            .first()
        )
        if active is not None:
            return active
        if cloud_vision_allowed is not None:
            document.cloud_vision_allowed = cloud_vision_allowed
        version.status = "PENDING"
        retry = KnowledgeIngestionJob(
            id=f"job_{uuid.uuid4().hex[:24]}",
            document_version_id=version.id,
            stage="PENDING",
            status="PENDING",
            trigger_actor=trigger_actor,
        )
        self.db.add(retry)
        self.db.commit()
        return retry

    def update_stage(self, job_id: str, stage: str, *, progress_page: int | None = None, total_pages: int | None = None) -> None:
        job = self.db.get(KnowledgeIngestionJob, job_id)
        if job is None:
            raise LookupError("摄取任务不存在")
        job.stage = stage
        job.status = "RUNNING"
        job.attempts += 1 if stage == "EXTRACTING" else 0
        job.started_at = job.started_at or _now()
        job.updated_at = _now()
        if progress_page is not None:
            job.progress_page = max(job.progress_page, progress_page)
        if total_pages is not None:
            job.total_pages = total_pages
        self.db.commit()

    def save_pages(self, version_id: str, pages: list[CanonicalPage]) -> None:
        self.db.query(KnowledgePage).filter_by(document_version_id=version_id).delete()
        for page in pages:
            self.db.add(
                KnowledgePage(
                    document_version_id=version_id,
                    page_number=page.page_number,
                    width_px=page.geometry.width_px,
                    height_px=page.geometry.height_px,
                    rotation=page.geometry.rotation,
                    text_strategy=page.route.text_strategy.value,
                    structure_strategy=page.route.structure_strategy.value,
                    route_reasons_json=json.dumps(page.route.reasons, ensure_ascii=False),
                    quality_json=page.quality.model_dump_json(),
                    status=page.route.status.value,
                    canonical_json=page.model_dump_json(),
                )
            )
        version = self.db.get(KnowledgeDocumentVersion, version_id)
        if version:
            version.page_count = len(pages)
        self.db.commit()

    def replace_chunks(self, version_id: str, drafts: list[ChunkDraft]) -> list[KnowledgeChunk]:
        self.db.query(KnowledgeChunk).filter_by(document_version_id=version_id).delete()
        parent_rows: dict[str, KnowledgeChunk] = {}
        rows: list[KnowledgeChunk] = []
        for draft in drafts:
            if draft.chunk_kind != ChunkKind.PARENT:
                continue
            row = self._chunk_row(draft, parent_chunk_id=None)
            self.db.add(row)
            parent_rows[draft.stable_id] = row
            rows.append(row)
        self.db.flush()
        for draft in drafts:
            if draft.chunk_kind == ChunkKind.PARENT:
                continue
            parent = parent_rows.get(draft.parent_stable_id or "")
            row = self._chunk_row(draft, parent_chunk_id=parent.id if parent else None)
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        return rows

    def version_chunks(self, version_id: str) -> list[KnowledgeChunk]:
        return (
            self.db.query(KnowledgeChunk)
            .filter_by(document_version_id=version_id)
            .order_by(KnowledgeChunk.source_index.asc())
            .all()
        )

    def activate(self, job_id: str, indexed_child_ids: set[str]) -> None:
        job, version, document = self.get_context(job_id)
        expected = {
            row.stable_id
            for row in self.db.query(KnowledgeChunk)
            .filter_by(document_version_id=version.id, chunk_kind=ChunkKind.CHILD.value)
            .all()
            if row.stable_id
        }
        if expected != indexed_child_ids:
            raise ValueError("索引 stable ID 集与数据库 child chunk 不一致")
        old_version_id = document.active_version_id
        self.db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document.id).update(
            {KnowledgeChunk.active: False}, synchronize_session=False
        )
        self.db.query(KnowledgeChunk).filter_by(document_version_id=version.id).update(
            {KnowledgeChunk.active: True}, synchronize_session=False
        )
        if old_version_id and old_version_id != version.id:
            old = self.db.get(KnowledgeDocumentVersion, old_version_id)
            if old:
                old.status = "SUPERSEDED"
        document.active_version_id = version.id
        document.updated_at = _now()
        version.status = "COMPLETED"
        version.completed_at = _now()
        job.stage = "COMPLETED"
        job.status = "COMPLETED"
        job.finished_at = _now()
        job.updated_at = _now()
        self.db.commit()

    def fail(self, job_id: str, *, status: str, code: str, message: str, retryable: bool) -> None:
        job = self.db.get(KnowledgeIngestionJob, job_id)
        if job is None:
            return
        job.status = status
        job.error_code = code
        job.error_message = message
        job.error_retryable = retryable
        job.finished_at = _now()
        job.updated_at = _now()
        version = self.db.get(KnowledgeDocumentVersion, job.document_version_id)
        if version:
            version.status = status
        self.db.commit()

    @staticmethod
    def _chunk_row(draft: ChunkDraft, parent_chunk_id: int | None) -> KnowledgeChunk:
        return KnowledgeChunk(
            source=draft.source,
            source_index=draft.source_index,
            content=draft.content,
            stable_id=draft.stable_id,
            document_id=draft.document_id,
            document_version_id=draft.document_version_id,
            parent_chunk_id=parent_chunk_id,
            chunk_kind=draft.chunk_kind.value,
            page_start=draft.page_start,
            page_end=draft.page_end,
            section_path_json=json.dumps(draft.section_path, ensure_ascii=False),
            block_ids_json=json.dumps(draft.block_ids, ensure_ascii=False),
            content_hash=draft.content_hash,
            active=False,
        )
