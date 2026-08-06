from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.chunking import ChunkingConfig, StructureAwareChunker
from app.rag_ingestion.fusion import PageEvidenceFusion
from app.rag_ingestion.pipeline import IngestionPipeline
from app.rag_ingestion.repository import KnowledgeIngestionRepository
from app.rag_ingestion.routing import PageRouter
from app.rag_ingestion.schema import (
    AccessClass,
    BlockType,
    EvidenceBlock,
    PageEvidence,
    PageFeatures,
    PageGeometry,
    ParseEvidence,
)


class FakeParser:
    calls = 0

    def parse_bytes(self, filename, data, context):
        self.calls += 1
        return ParseEvidence(
            filename=filename,
            mime_type="text/plain",
            parser_name="fake",
            parser_version="1",
            pages=[
                PageEvidence(
                    page_number=1,
                    geometry=PageGeometry(width_px=1000, height_px=1400),
                    image_path="",
                    native_blocks=[
                        EvidenceBlock(
                            evidence_id="e1",
                            type=BlockType.PARAGRAPH,
                            bbox_norm=[0.1, 0.1, 0.9, 0.2],
                            text="压力来临时先缓慢呼吸，再联系可信任的人。",
                            confidence=1.0,
                            section_path=["压力管理"],
                        )
                    ],
                    features=PageFeatures(native_character_count=22, native_text_score=1.0),
                )
            ],
        )


class ForbiddenProvider:
    def __init__(self):
        self.calls = 0

    def recognize(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("简单原生页不应调用 OCR")

    def analyze(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("简单原生页不应调用 Vision")


class RecordingIndexer:
    def __init__(self):
        self.calls = 0

    def index(self, chunks):
        self.calls += 1
        return {chunk.stable_id for chunk in chunks if chunk.chunk_kind == "CHILD"}


def test_pipeline_runs_native_local_path_and_activates_only_after_index_validation(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repository = KnowledgeIngestionRepository(db)
        submission = repository.ensure_submission(
            source_key="admin:notes.txt", display_name="notes.txt", mime_type="text/plain",
            sha256="a" * 64, size_bytes=7, access_class=AccessClass.ADMIN_PRIVATE,
            cloud_vision_allowed=False, trigger_actor="admin", pipeline_fingerprint="rag-v1",
        )
        store = ArtifactStore(tmp_path)
        store.write_source(submission.document.id, submission.version.sha256, b"content")
        parser = FakeParser()
        blocked = ForbiddenProvider()
        indexer = RecordingIndexer()
        pipeline = IngestionPipeline(
            repository=repository,
            artifact_store=store,
            parsers={"text/plain": parser},
            router=PageRouter(),
            ocr_provider=blocked,
            vision_provider=blocked,
            fusion=PageEvidenceFusion(),
            chunker=StructureAwareChunker(ChunkingConfig(child_min_tokens=1)),
            indexer=indexer,
        )

        pipeline.run(submission.job.id)

        db.refresh(submission.job)
        db.refresh(submission.document)
        assert submission.job.status == "COMPLETED"
        assert submission.document.active_version_id == submission.version.id
        assert blocked.calls == 0
        assert indexer.calls == 1

        pipeline.run(submission.job.id)
        assert parser.calls == 1
        assert indexer.calls == 2
    engine.dispose()
