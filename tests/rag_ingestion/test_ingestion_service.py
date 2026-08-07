from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, OutboxEvent
from app.rag_ingestion.schema import AccessClass
from app.rag_ingestion.service import KnowledgeChunkIndexer, KnowledgeIngestionService


def make_service(tmp_path: Path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    calls: list[str] = []
    settings = Settings(
        app_environment="test",
        rag_artifact_dir=str(tmp_path / "artifacts"),
        knowledge_vector_enabled=False,
    )
    service = KnowledgeIngestionService(
        db,
        settings,
        task_dispatcher=lambda job_id: calls.append(job_id),
    )
    return engine, db, service, calls


def test_submit_file_is_deduplicated_and_private_cloud_is_opt_in(tmp_path):
    engine, db, service, calls = make_service(tmp_path)
    try:
        first = service.submit_file(
            filename="guide.pdf",
            data=b"%PDF-1.4\nfixture",
            mime_type="application/pdf",
            actor="admin",
            access_class=AccessClass.ADMIN_PRIVATE,
        )
        second = service.submit_file(
            filename="guide.pdf",
            data=b"%PDF-1.4\nfixture",
            mime_type="application/pdf",
            actor="admin",
            access_class=AccessClass.ADMIN_PRIVATE,
        )

        assert first.job_id == second.job_id
        assert first.created is True
        assert second.created is False
        assert calls == [first.job_id]
        assert first.cloud_vision_allowed is False
        assert Path(first.source_path).read_bytes() == b"%PDF-1.4\nfixture"
    finally:
        db.close()
        engine.dispose()


def test_default_submission_records_transactional_outbox_event(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = Settings(
        app_environment="test",
        rag_artifact_dir=str(tmp_path / "artifacts"),
        knowledge_vector_enabled=False,
    )
    try:
        submitted = KnowledgeIngestionService(db, settings).submit_file(
            filename="async.pdf",
            data=b"%PDF-1.4\nfixture",
            mime_type="application/pdf",
            actor="admin",
            access_class=AccessClass.ADMIN_PRIVATE,
        )

        event = db.query(OutboxEvent).one()
        assert event.event_type == "knowledge.ingest"
        assert event.aggregate_type == "knowledge_ingestion_job"
        assert event.aggregate_id == submitted.job_id
        assert event.idempotency_key == f"knowledge.ingest:{submitted.job_id}"
        assert f'"jobId":"{submitted.job_id}"' in event.payload_json
        assert event.status == "PENDING"
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("filename", "mime_type", "data"),
    [
        ("guide.exe", "application/octet-stream", b"x"),
        ("guide.pdf", "text/plain", b"%PDF-1.4"),
        ("empty.txt", "text/plain", b""),
    ],
)
def test_submit_file_rejects_unsupported_or_mismatched_input(
    tmp_path, filename, mime_type, data
):
    engine, db, service, _ = make_service(tmp_path)
    try:
        with pytest.raises(ValueError):
            service.submit_file(
                filename=filename,
                data=data,
                mime_type=mime_type,
                actor="admin",
                access_class=AccessClass.ADMIN_PRIVATE,
            )
    finally:
        db.close()
        engine.dispose()


def test_retry_only_allows_retryable_failure(tmp_path):
    engine, db, service, calls = make_service(tmp_path)
    try:
        submitted = service.submit_file(
            filename="notes.txt",
            data=b"safe content",
            mime_type="text/plain",
            actor="admin",
            access_class=AccessClass.ADMIN_PRIVATE,
        )
        service.repository.fail(
            submitted.job_id,
            status="FAILED",
            code="TEMPORARY",
            message="temporary failure",
            retryable=True,
        )
        retried = service.retry(submitted.job_id, actor="admin")
        assert retried.job_id != submitted.job_id
        assert retried.status == "PENDING"
        assert calls == [submitted.job_id, retried.job_id]
    finally:
        db.close()
        engine.dispose()


def test_indexer_embeds_only_child_chunks_and_returns_exact_stable_ids():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine)

    class FakeVectorStore:
        can_embed = True
        error = ""

        def __init__(self):
            self.rows = []

        def embed_texts(self, texts):
            assert texts == ["searchable child"]
            return [[0.1, 0.2, 0.3]]

        def upsert_chunks(self, rows, embeddings):
            self.rows = rows
            assert embeddings == [[0.1, 0.2, 0.3]]
            return len(rows)

    try:
        parent = KnowledgeChunk(
            source="guide.pdf",
            source_index=0,
            content="parent context",
            stable_id="parent_1",
            chunk_kind="PARENT",
            active=False,
        )
        child = KnowledgeChunk(
            source="guide.pdf",
            source_index=1,
            content="searchable child",
            stable_id="child_1",
            chunk_kind="CHILD",
            active=False,
        )
        db.add_all([parent, child])
        db.commit()
        vector = FakeVectorStore()
        indexed = KnowledgeChunkIndexer(
            db,
            Settings(app_environment="test", knowledge_vector_enabled=True),
            vector_store=vector,
        ).index([parent, child])
        assert indexed == {"child_1"}
        assert vector.rows == [child]
        assert child.embedding_model == "text-embedding-3-small"
        assert child.embedding_dimension == 3
    finally:
        db.close()
        engine.dispose()
