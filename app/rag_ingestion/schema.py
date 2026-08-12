from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


NormalizedBbox = Annotated[list[float], Field(min_length=4, max_length=4)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class AccessClass(StrEnum):
    BUILTIN_PUBLIC = "BUILTIN_PUBLIC"
    ADMIN_PRIVATE = "ADMIN_PRIVATE"
    RESTRICTED = "RESTRICTED"


class TextStrategy(StrEnum):
    NATIVE = "NATIVE"
    PADDLE_OCR = "PADDLE_OCR"
    HYBRID = "HYBRID"


class StructureStrategy(StrEnum):
    LOCAL = "LOCAL"
    VISION = "VISION"


class BlockType(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    LIST_ITEM = "list_item"
    CODE = "code"
    TABLE = "table"
    TABLE_CELL = "table_cell"
    FIGURE = "figure"
    CAPTION = "caption"
    COMIC_PANEL = "comic_panel"
    EQUATION = "equation"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"


class ChunkKind(StrEnum):
    PARENT = "PARENT"
    CHILD = "CHILD"
    LEGACY_TEXT = "LEGACY_TEXT"


class PageStatus(StrEnum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class PageGeometry(StrictModel):
    width_px: int = Field(gt=0)
    height_px: int = Field(gt=0)
    rotation: int = 0


class PageRoute(StrictModel):
    text_strategy: TextStrategy
    structure_strategy: StructureStrategy
    reasons: list[str] = Field(default_factory=list)
    router_version: str = "page-router-v1"
    status: PageStatus = PageStatus.READY


class PageQuality(StrictModel):
    native_text_score: float = Field(default=0.0, ge=0.0, le=1.0)
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    layout_complexity: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_semantic_need: float = Field(default=0.0, ge=0.0, le=1.0)
    final_page_score: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class EvidenceProvenance(StrictModel):
    provider: Literal["native", "paddleocr", "vision"]
    provider_version: str = ""
    source_block_id: str | None = None
    bbox_norm: NormalizedBbox | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_text: str | None = None

    @model_validator(mode="after")
    def validate_bbox(self):
        if self.bbox_norm is not None:
            _validate_bbox(self.bbox_norm)
        return self


class TableCell(StrictModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    text: str
    rowspan: int = Field(default=1, ge=1)
    colspan: int = Field(default=1, ge=1)
    bbox_norm: NormalizedBbox | None = None

    @model_validator(mode="after")
    def validate_bbox(self):
        if self.bbox_norm is not None:
            _validate_bbox(self.bbox_norm)
        return self


class TableData(StrictModel):
    rows: int = Field(ge=0)
    columns: int = Field(ge=0)
    cells: list[TableCell] = Field(default_factory=list)


class FigureData(StrictModel):
    asset_path: str | None = None
    caption: str = ""
    ocr_text: str = ""
    description: str = ""


class EvidenceBlock(StrictModel):
    evidence_id: str
    type: BlockType = BlockType.PARAGRAPH
    bbox_norm: NormalizedBbox
    text: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    section_path: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bbox(self):
        _validate_bbox(self.bbox_norm)
        return self


class PageFeatures(StrictModel):
    native_character_count: int = Field(default=0, ge=0)
    native_text_score: float = Field(default=0.0, ge=0.0, le=1.0)
    garbled_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    image_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    layout_complexity: float = Field(default=0.0, ge=0.0, le=1.0)
    visual_semantic_need: float = Field(default=0.0, ge=0.0, le=1.0)
    multi_column: bool = False
    table_candidate: bool = False
    comic_candidate: bool = False
    infographic_candidate: bool = False
    image_text_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    liteparse_needs_ocr: bool = False
    liteparse_reasons: list[str] = Field(default_factory=list)


class PageEvidence(StrictModel):
    page_number: int = Field(ge=1)
    geometry: PageGeometry
    image_path: str
    native_blocks: list[EvidenceBlock] = Field(default_factory=list)
    features: PageFeatures = Field(default_factory=PageFeatures)


class ParseEvidence(StrictModel):
    filename: str
    mime_type: str
    parser_name: str
    parser_version: str
    pages: list[PageEvidence]


class OcrLine(StrictModel):
    text: str
    polygon_norm: list[list[float]] = Field(min_length=4)
    confidence: float = Field(ge=0.0, le=1.0)


class OcrPageEvidence(StrictModel):
    page_number: int = Field(ge=1)
    lines: list[OcrLine]
    average_confidence: float = Field(ge=0.0, le=1.0)
    minimum_confidence: float = Field(ge=0.0, le=1.0)
    provider: str = "paddleocr"
    provider_version: str = ""
    model_name: str = ""
    device: str = "cpu"
    elapsed_ms: int = Field(default=0, ge=0)


class VisionBlock(StrictModel):
    type: BlockType
    reading_order: int = Field(ge=0)
    bbox_norm: NormalizedBbox
    text: str = ""
    section_path: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    table: TableData | None = None
    figure: FigureData | None = None

    @model_validator(mode="after")
    def validate_bbox(self):
        _validate_bbox(self.bbox_norm)
        return self


class VisionAnalysisRequest(StrictModel):
    page_number: int = Field(ge=1)
    image_path: Path
    access_class: AccessClass
    cloud_vision_allowed: bool
    native_text: str = ""
    ocr_text: str = ""


class VisionPageResult(StrictModel):
    page_number: int = Field(ge=1)
    page_type: str = "unknown"
    blocks: list[VisionBlock]
    warnings: list[str] = Field(default_factory=list)


class CanonicalBlock(StrictModel):
    block_id: str
    type: BlockType
    reading_order: int = Field(ge=0)
    bbox_norm: NormalizedBbox
    text: str = ""
    section_path: list[str] = Field(default_factory=list)
    parent_block_id: str | None = None
    child_block_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    searchable: bool = True
    provenance: list[EvidenceProvenance]
    table: TableData | None = None
    figure: FigureData | None = None

    @model_validator(mode="after")
    def validate_bbox(self):
        _validate_bbox(self.bbox_norm)
        return self


class CanonicalPage(StrictModel):
    page_number: int = Field(ge=1)
    geometry: PageGeometry
    route: PageRoute
    quality: PageQuality
    blocks: list[CanonicalBlock]

    @model_validator(mode="after")
    def validate_reading_order(self):
        orders = [block.reading_order for block in self.blocks]
        if len(orders) != len(set(orders)):
            raise ValueError("reading_order 不得重复")
        if orders and sorted(orders) != list(range(len(orders))):
            raise ValueError("reading_order 必须从 0 连续递增")
        return self


class SourceInfo(StrictModel):
    filename: str
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    access_class: AccessClass
    cloud_vision_allowed: bool


class ParserRunInfo(StrictModel):
    pipeline_version: str = "rag-ingestion-v3"
    router_version: str = "page-router-v1"
    chunker_version: str = "structure-chunker-v3"
    liteparse_version: str = ""
    ocr_provider: str = "paddleocr"
    ocr_model: str = ""
    vision_provider: str = "openai_compatible"
    vision_model: str = "gpt-5.6-luna"


class CanonicalDocument(StrictModel):
    schema_version: str = "1.0"
    document_id: str
    version_id: str
    source: SourceInfo
    parser_run: ParserRunInfo
    pages: list[CanonicalPage]


class ChunkDraft(StrictModel):
    stable_id: str
    document_id: str
    document_version_id: str
    parent_stable_id: str | None = None
    chunk_kind: ChunkKind
    source: str
    source_index: int = Field(ge=0)
    content: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    section_path: list[str] = Field(default_factory=list)
    block_ids: list[str] = Field(default_factory=list)
    content_hash: str


def _validate_bbox(bbox: list[float]) -> None:
    x0, y0, x1, y1 = bbox
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError("bbox_norm 必须位于 0..1 且满足 x0<x1、y0<y1")
