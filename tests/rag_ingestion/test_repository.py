from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import KnowledgeChunk
from app.rag_ingestion.repository import KnowledgeIngestionRepository
from app.rag_ingestion.schema import AccessClass


def test_repository_reuses_same_document_hash_and_active_job():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repository = KnowledgeIngestionRepository(db)
        first = repository.ensure_submission(
            source_key="builtin:guide.pdf",
            display_name="guide.pdf",
            mime_type="application/pdf",
            sha256="a" * 64,
            size_bytes=100,
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
            trigger_actor="bootstrap",
            pipeline_fingerprint="rag-v1",
        )
        second = repository.ensure_submission(
            source_key="builtin:guide.pdf",
            display_name="guide.pdf",
            mime_type="application/pdf",
            sha256="a" * 64,
            size_bytes=100,
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
            trigger_actor="bootstrap",
            pipeline_fingerprint="rag-v1",
        )

        assert first.document.id == second.document.id
        assert first.version.id == second.version.id
        assert first.job.id == second.job.id
        assert first.created is True
        assert second.created is False
    engine.dispose()


def test_activation_switches_versions_only_after_new_chunks_exist():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repository = KnowledgeIngestionRepository(db)
        first = repository.ensure_submission(
            source_key="builtin:guide.pdf", display_name="guide.pdf", mime_type="application/pdf",
            sha256="a" * 64, size_bytes=100, access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True, trigger_actor="bootstrap", pipeline_fingerprint="rag-v1",
        )
        old = KnowledgeChunk(
            source="guide.pdf", source_index=0, content="旧内容", stable_id="chunk_old",
            document_id=first.document.id, document_version_id=first.version.id,
            chunk_kind="CHILD", active=True,
        )
        db.add(old)
        db.commit()
        repository.activate(first.job.id, {"chunk_old"})

        second = repository.ensure_submission(
            source_key="builtin:guide.pdf", display_name="guide.pdf", mime_type="application/pdf",
            sha256="b" * 64, size_bytes=101, access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True, trigger_actor="bootstrap", pipeline_fingerprint="rag-v1",
        )
        new = KnowledgeChunk(
            source="guide.pdf", source_index=0, content="新内容", stable_id="chunk_new",
            document_id=second.document.id, document_version_id=second.version.id,
            chunk_kind="CHILD", active=False,
        )
        db.add(new)
        db.commit()
        repository.activate(second.job.id, {"chunk_new"})

        assert db.query(KnowledgeChunk).filter_by(stable_id="chunk_old").one().active is False
        assert db.query(KnowledgeChunk).filter_by(stable_id="chunk_new").one().active is True
        assert second.document.active_version_id == second.version.id
    engine.dispose()


def test_successful_activation_clears_error_left_by_an_automatic_retry():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repository = KnowledgeIngestionRepository(db)
        submission = repository.ensure_submission(
            source_key="builtin:retry.pdf",
            display_name="retry.pdf",
            mime_type="application/pdf",
            sha256="c" * 64,
            size_bytes=100,
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
            trigger_actor="bootstrap",
            pipeline_fingerprint="rag-v2",
        )
        chunk = KnowledgeChunk(
            source="retry.pdf",
            source_index=0,
            content="重试后成功的内容",
            stable_id="chunk_retry",
            document_id=submission.document.id,
            document_version_id=submission.version.id,
            chunk_kind="CHILD",
            active=False,
        )
        submission.job.error_code = "VISION_UPSTREAM_RETRYABLE"
        submission.job.error_message = "Vision 上游暂时不可用"
        submission.job.error_retryable = True
        db.add(chunk)
        db.commit()

        repository.activate(submission.job.id, {"chunk_retry"})

        assert submission.job.status == "COMPLETED"
        assert submission.job.error_code is None
        assert submission.job.error_message is None
        assert submission.job.error_retryable is False
    engine.dispose()
