from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.core.database import Base
from app.models.entities import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIngestionJob,
    KnowledgePage,
)


def test_rag_models_round_trip_versioned_document_and_page():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        document = KnowledgeDocument(
            id="doc_1",
            source_key="builtin:guide.pdf",
            display_name="guide.pdf",
            mime_type="application/pdf",
            access_class="BUILTIN_PUBLIC",
            cloud_vision_allowed=True,
        )
        version = KnowledgeDocumentVersion(
            id="docver_1",
            document_id="doc_1",
            sha256="a" * 64,
            size_bytes=100,
            status="PENDING",
            pipeline_fingerprint="pipeline-v1",
        )
        job = KnowledgeIngestionJob(
            id="job_1",
            document_version_id="docver_1",
            stage="PENDING",
            status="PENDING",
            trigger_actor="admin",
        )
        page = KnowledgePage(
            document_version_id="docver_1",
            page_number=1,
            width_px=1000,
            height_px=1400,
            text_strategy="NATIVE",
            structure_strategy="LOCAL",
            status="READY",
            canonical_json='{"page_number":1}',
        )
        db.add_all([document, version, job, page])
        db.commit()

        assert db.get(KnowledgeDocument, "doc_1").display_name == "guide.pdf"
        assert db.query(KnowledgePage).one().page_number == 1

    columns = {item["name"] for item in inspect(engine).get_columns("knowledge_chunks")}
    assert {"stable_id", "document_id", "document_version_id", "parent_chunk_id",
            "chunk_kind", "page_start", "page_end", "active"} <= columns
    engine.dispose()


def test_legacy_chunk_defaults_to_active_legacy_text():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        chunk = KnowledgeChunk(source="legacy.md", source_index=0, content="正文")
        db.add(chunk)
        db.commit()
        db.refresh(chunk)
        assert chunk.active is True
        assert chunk.chunk_kind == "LEGACY_TEXT"
    engine.dispose()
