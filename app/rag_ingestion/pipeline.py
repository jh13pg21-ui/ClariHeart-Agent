from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.models.entities import KnowledgeChunk
from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.chunking import StructureAwareChunker
from app.rag_ingestion.errors import IngestionError, NeedsReviewError, sanitize_error
from app.rag_ingestion.fusion import PageEvidenceFusion
from app.rag_ingestion.parsers.base import DocumentParser, ParserContext
from app.rag_ingestion.repository import KnowledgeIngestionRepository
from app.rag_ingestion.routing import PageRouter
from app.rag_ingestion.schema import (
    AccessClass,
    CanonicalDocument,
    OcrPageEvidence,
    PageStatus,
    ParserRunInfo,
    SourceInfo,
    StructureStrategy,
    TextStrategy,
    VisionAnalysisRequest,
    VisionPageResult,
)


class ChunkIndexer(Protocol):
    def index(self, chunks: list[KnowledgeChunk]) -> set[str]: ...


class IngestionPipeline:
    def __init__(
        self,
        *,
        repository: KnowledgeIngestionRepository,
        artifact_store: ArtifactStore,
        parsers: dict[str, DocumentParser],
        router: PageRouter,
        ocr_provider,
        vision_provider,
        fusion: PageEvidenceFusion,
        chunker: StructureAwareChunker,
        indexer: ChunkIndexer,
        max_pages: int = 500,
    ):
        self.repository = repository
        self.artifact_store = artifact_store
        self.parsers = parsers
        self.router = router
        self.ocr_provider = ocr_provider
        self.vision_provider = vision_provider
        self.fusion = fusion
        self.chunker = chunker
        self.indexer = indexer
        self.max_pages = max_pages

    def run(self, job_id: str) -> None:
        try:
            job, version, document = self.repository.get_context(job_id)
            canonical_path = self.artifact_store.version_dir(document.id, version.sha256) / "document.json"
            if canonical_path.exists():
                canonical = CanonicalDocument.model_validate(
                    self.artifact_store.read_json(document.id, version.sha256, "document.json")
                )
            else:
                canonical = self._extract_and_fuse(job_id, version, document)
                self.artifact_store.write_json(document.id, version.sha256, "document.json", canonical)
                version.canonical_artifact_path = str(canonical_path)
                self.repository.db.commit()

            self.repository.update_stage(job_id, "CHUNKING", total_pages=len(canonical.pages))
            drafts = self.chunker.chunk(canonical)
            rows = self.repository.version_chunks(version.id)
            if not rows:
                rows = self.repository.replace_chunks(version.id, drafts)
            self.repository.update_stage(job_id, "INDEXING", total_pages=len(canonical.pages))
            indexed_ids = self.indexer.index(rows)
            self.repository.update_stage(job_id, "ACTIVATING", total_pages=len(canonical.pages))
            self.repository.activate(job_id, indexed_ids)
        except NeedsReviewError as exc:
            code, message, retryable = sanitize_error(exc)
            self.repository.fail(job_id, status="NEEDS_REVIEW", code=code, message=message, retryable=retryable)
            raise
        except Exception as exc:
            code, message, retryable = sanitize_error(exc)
            self.repository.fail(job_id, status="FAILED", code=code, message=message, retryable=retryable)
            raise

    def _extract_and_fuse(self, job_id, version, document) -> CanonicalDocument:
        self.repository.update_stage(job_id, "EXTRACTING")
        parser = self.parsers.get(document.mime_type)
        if parser is None:
            raise ValueError(f"不支持的文档 MIME: {document.mime_type}")
        source_path = self.artifact_store.version_dir(document.id, version.sha256) / "source.bin"
        data = source_path.read_bytes()
        context = ParserContext(
            document_id=document.id,
            version_id=version.id,
            access_class=AccessClass(document.access_class),
            cloud_vision_allowed=document.cloud_vision_allowed,
            source_sha256=version.sha256,
        )
        parsed = parser.parse_bytes(document.display_name, data, context)
        if len(parsed.pages) > self.max_pages:
            raise IngestionError(
                f"文档页数 {len(parsed.pages)} 超过限制 {self.max_pages}",
                code="PAGE_LIMIT_EXCEEDED",
            )
        self.repository.update_stage(job_id, "ROUTING", total_pages=len(parsed.pages))
        canonical_pages = []
        for page in parsed.pages:
            route = self.router.route(page, context.access_class, context.cloud_vision_allowed)
            if route.status == PageStatus.NEEDS_REVIEW:
                raise NeedsReviewError(f"第 {page.page_number} 页需要 Vision，但云端发送未获授权")
            ocr = self._ocr(job_id, document.id, version.sha256, page, route)
            if ocr and route.text_strategy == TextStrategy.PADDLE_OCR:
                route = self.router.escalate_after_ocr(
                    route,
                    ocr_confidence=ocr.average_confidence,
                    coverage_score=1.0 if ocr.lines else 0.0,
                    cloud_vision_allowed=context.cloud_vision_allowed,
                )
            if route.status == PageStatus.NEEDS_REVIEW:
                raise NeedsReviewError(f"第 {page.page_number} 页 OCR 质量不足且云端发送未获授权")
            vision = self._vision(job_id, context, page, route, ocr)
            self.repository.update_stage(
                job_id,
                "FUSING",
                progress_page=page.page_number,
                total_pages=len(parsed.pages),
            )
            canonical_pages.append(self.fusion.fuse(version.id, page, route, ocr=ocr, vision=vision))
        canonical = CanonicalDocument(
            document_id=document.id,
            version_id=version.id,
            source=SourceInfo(
                filename=document.display_name,
                mime_type=document.mime_type,
                sha256=version.sha256,
                size_bytes=version.size_bytes,
                access_class=context.access_class,
                cloud_vision_allowed=context.cloud_vision_allowed,
            ),
            parser_run=ParserRunInfo(
                liteparse_version=parsed.parser_version if parsed.parser_name == "liteparse" else "",
            ),
            pages=canonical_pages,
        )
        self.repository.save_pages(version.id, canonical_pages)
        return canonical

    def _ocr(self, job_id, document_id, sha256, page, route) -> OcrPageEvidence | None:
        if route.text_strategy not in {TextStrategy.PADDLE_OCR, TextStrategy.HYBRID}:
            return None
        name = f"pages/{page.page_number:04d}.ocr.json"
        path = self.artifact_store.version_dir(document_id, sha256) / name
        if path.exists():
            return OcrPageEvidence.model_validate(self.artifact_store.read_json(document_id, sha256, name))
        self.repository.update_stage(job_id, "OCR_RUNNING", progress_page=page.page_number)
        result = self.ocr_provider.recognize(page.image_path, page_number=page.page_number)
        self.artifact_store.write_json(document_id, sha256, name, result)
        return result

    def _vision(self, job_id, context, page, route, ocr) -> VisionPageResult | None:
        if route.structure_strategy != StructureStrategy.VISION:
            return None
        name = f"pages/{page.page_number:04d}.vision.json"
        path = self.artifact_store.version_dir(context.document_id, context.source_sha256) / name
        if path.exists():
            return VisionPageResult.model_validate(
                self.artifact_store.read_json(context.document_id, context.source_sha256, name)
            )
        self.repository.update_stage(job_id, "VISION_RUNNING", progress_page=page.page_number)
        request = VisionAnalysisRequest(
            page_number=page.page_number,
            image_path=Path(page.image_path),
            access_class=context.access_class,
            cloud_vision_allowed=context.cloud_vision_allowed,
            native_text="\n".join(block.text for block in page.native_blocks),
            ocr_text="\n".join(line.text for line in ocr.lines) if ocr else "",
        )
        result = self.vision_provider.analyze(request)
        self.artifact_store.write_json(context.document_id, context.source_sha256, name, result)
        return result
