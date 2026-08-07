from __future__ import annotations

import importlib.metadata
from collections.abc import Callable

from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.ids import stable_block_id
from app.rag_ingestion.parsers.base import ParserContext
from app.rag_ingestion.schema import (
    BlockType,
    EvidenceBlock,
    PageEvidence,
    PageFeatures,
    PageGeometry,
    ParseEvidence,
)


class LiteParseDocumentParser:
    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        parser_factory: Callable[..., object] | None = None,
        dpi: int = 150,
        ocr_enabled: bool = False,
    ):
        self.artifact_store = artifact_store
        self.parser_factory = parser_factory or self._default_factory
        self.dpi = dpi
        self.ocr_enabled = ocr_enabled

    def parse_bytes(self, filename: str, data: bytes, context: ParserContext) -> ParseEvidence:
        if not context.source_sha256:
            raise ValueError("PDF parser 需要 source_sha256")
        self.artifact_store.write_source(context.document_id, context.source_sha256, data)
        parser = self.parser_factory(
            ocr_enabled=self.ocr_enabled,
            dpi=float(self.dpi),
            output_format="json",
            keep_headers_footers=True,
            include_complexity=True,
            extract_vector_graphics=True,
            emit_word_boxes=True,
            quiet=True,
        )
        parsed = parser.parse(data)
        with self.artifact_store.parser_input(
            context.document_id,
            context.source_sha256,
            data,
            ".pdf",
        ) as render_source:
            screenshots = {item.page_num: item for item in parser.screenshot(render_source)}
        pages = []
        for page in parsed.pages:
            page_number = int(page.page_num)
            screenshot = screenshots[page_number]
            image_path = self.artifact_store.write_page_bytes(
                context.document_id,
                context.source_sha256,
                page_number,
                bytes(screenshot.image_bytes),
            )
            blocks = [
                EvidenceBlock(
                    evidence_id=stable_block_id(context.version_id, page_number, index, item.text),
                    type=BlockType.PARAGRAPH,
                    bbox_norm=self._bbox(item.x, item.y, item.width, item.height, page.width, page.height),
                    text=item.text,
                    confidence=float(getattr(item, "confidence", None) or 1.0),
                    metadata={"font_name": str(getattr(item, "font_name", ""))},
                )
                for index, item in enumerate(page.text_items)
                if str(item.text).strip() and item.width > 0 and item.height > 0
            ]
            features = self._features(page, blocks)
            page_evidence = PageEvidence(
                page_number=page_number,
                geometry=PageGeometry(
                    width_px=int(screenshot.width),
                    height_px=int(screenshot.height),
                ),
                image_path=str(image_path),
                native_blocks=blocks,
                features=features,
            )
            self.artifact_store.write_json(
                context.document_id,
                context.source_sha256,
                f"pages/{page_number:04d}.native.json",
                page_evidence,
            )
            pages.append(page_evidence)
        return ParseEvidence(
            filename=filename,
            mime_type="application/pdf",
            parser_name="liteparse",
            parser_version=self._version(),
            pages=pages,
        )

    @staticmethod
    def _default_factory(**kwargs):
        from liteparse import LiteParse

        return LiteParse(**kwargs)

    @staticmethod
    def _version() -> str:
        try:
            return importlib.metadata.version("liteparse")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    @staticmethod
    def _bbox(x: float, y: float, width: float, height: float, page_width: float, page_height: float) -> list[float]:
        safe_width = max(float(page_width), 1.0)
        safe_height = max(float(page_height), 1.0)
        values = [x / safe_width, y / safe_height, (x + width) / safe_width, (y + height) / safe_height]
        return [max(0.0, min(1.0, float(value))) for value in values]

    @staticmethod
    def _features(page, blocks: list[EvidenceBlock]) -> PageFeatures:
        complexity = getattr(page, "complexity", None)
        layout = getattr(complexity, "layout", None)
        text = "".join(block.text for block in blocks)
        character_count = len(text)
        replacement_count = text.count("�")
        garbled_ratio = replacement_count / max(1, character_count)
        image_coverage = float(getattr(complexity, "image_coverage", 0.0) or 0.0)
        column_count = int(getattr(layout, "column_count", 1) or 1)
        ruled_tables = int(getattr(layout, "ruled_table_count", 0) or 0)
        figures = int(getattr(layout, "figure_count", 0) or 0)
        is_complex = bool(getattr(layout, "is_complex", False))
        layout_score = min(1.0, 0.35 * max(0, column_count - 1) + 0.4 * bool(ruled_tables) + 0.25 * is_complex)
        native_score = min(1.0, character_count / 80.0) * (1.0 - min(1.0, garbled_ratio * 4.0))
        visual_need = min(1.0, image_coverage + 0.25 * bool(figures) + 0.35 * bool(ruled_tables))
        reasons = list(getattr(complexity, "reasons", []) or []) + list(getattr(layout, "reasons", []) or [])
        return PageFeatures(
            native_character_count=character_count,
            native_text_score=native_score,
            garbled_ratio=garbled_ratio,
            image_coverage=max(0.0, min(1.0, image_coverage)),
            layout_complexity=layout_score,
            visual_semantic_need=visual_need,
            multi_column=column_count > 1,
            table_candidate=ruled_tables > 0,
            image_text_risk=min(1.0, image_coverage * (1.0 - native_score)),
            liteparse_needs_ocr=bool(getattr(complexity, "needs_ocr", False)),
            liteparse_reasons=[str(item) for item in reasons],
        )
