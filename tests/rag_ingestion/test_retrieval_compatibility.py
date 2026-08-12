from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion
from app.services.knowledge import KnowledgeService


def test_retrieval_uses_active_child_then_expands_parent_with_citations():
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
            active_version_id="docver_1",
        )
        version = KnowledgeDocumentVersion(
            id="docver_1",
            document_id="doc_1",
            sha256="a" * 64,
            size_bytes=10,
            status="COMPLETED",
            pipeline_fingerprint="rag-v1",
        )
        parent = KnowledgeChunk(
            source="guide.pdf",
            source_index=0,
            content="Complete section: slow breathing helps restore attention.",
            stable_id="parent_1",
            document_id="doc_1",
            document_version_id="docver_1",
            chunk_kind="PARENT",
            page_start=3,
            page_end=4,
            active=True,
        )
        db.add_all([document, version, parent])
        db.flush()
        child = KnowledgeChunk(
            source="guide.pdf",
            source_index=1,
            content="slow breathing",
            stable_id="child_1",
            document_id="doc_1",
            document_version_id="docver_1",
            parent_chunk_id=parent.id,
            chunk_kind="CHILD",
            page_start=3,
            page_end=3,
            section_path_json='["Stress management", "Breathing"]',
            block_ids_json='["b1"]',
            active=True,
        )
        inactive = KnowledgeChunk(
            source="old.pdf",
            source_index=0,
            content="slow breathing slow breathing slow breathing",
            stable_id="old_1",
            document_id="doc_1",
            document_version_id="docver_1",
            chunk_kind="CHILD",
            page_start=9,
            page_end=9,
            active=False,
        )
        db.add_all([child, inactive])
        db.commit()

        service = KnowledgeService(
            db,
            Settings(
                app_environment="test",
                knowledge_vector_enabled=False,
                knowledge_top_k=1,
            ),
        )
        result = service.retrieve("slow breathing", top_k=1)[0]

        assert result.chunk_id == child.id
        assert result.content == parent.content
        assert result.document_id == "doc_1"
        assert result.filename == "guide.pdf"
        assert result.page_start == 3
        assert result.page_end == 3
        assert result.section_path == ["Stress management", "Breathing"]
        assert result.block_ids == ["b1"]
        assert service.count() == 1
    engine.dispose()


def test_legacy_active_chunks_remain_searchable_during_rollout():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            KnowledgeChunk(
                source="legacy.md",
                source_index=0,
                content="grounding exercise for test anxiety",
            )
        )
        db.commit()
        result = KnowledgeService(
            db,
            Settings(app_environment="test", knowledge_vector_enabled=False),
        ).retrieve("grounding exercise", top_k=1)[0]
        assert result.source == "legacy.md"
        assert result.document_id is None
    engine.dispose()
