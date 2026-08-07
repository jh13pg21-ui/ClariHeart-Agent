from __future__ import annotations

from app.rag_ingestion.fusion import PageEvidenceFusion
from app.rag_ingestion.schema import (
    BlockType,
    EvidenceBlock,
    OcrLine,
    OcrPageEvidence,
    PageEvidence,
    PageFeatures,
    PageGeometry,
    PageRoute,
    StructureStrategy,
    TextStrategy,
    VisionBlock,
    VisionPageResult,
)


def native_page(text="政策编号 12345"):
    return PageEvidence(
        page_number=1,
        geometry=PageGeometry(width_px=1000, height_px=1400),
        image_path="page.png",
        native_blocks=[
            EvidenceBlock(
                evidence_id="native-1",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.1, 0.1, 0.9, 0.2],
                text=text,
                confidence=1.0,
            )
        ],
        features=PageFeatures(native_text_score=0.95),
    )


def test_vision_controls_structure_but_cannot_replace_verified_native_number():
    vision = VisionPageResult(
        page_number=1,
        page_type="policy",
        blocks=[
            VisionBlock(
                type=BlockType.HEADING,
                reading_order=0,
                bbox_norm=[0.1, 0.1, 0.9, 0.2],
                text="政策编号 54321",
                confidence=0.9,
            )
        ],
    )
    page = PageEvidenceFusion().fuse(
        "docver_1",
        native_page(),
        PageRoute(text_strategy=TextStrategy.NATIVE, structure_strategy=StructureStrategy.VISION),
        vision=vision,
    )
    assert page.blocks[0].type == BlockType.HEADING
    assert page.blocks[0].text == "政策编号 12345"
    assert {item.provider for item in page.blocks[0].provenance} == {"native", "vision"}


def test_hybrid_route_adds_non_overlapping_ocr_text():
    ocr = OcrPageEvidence(
        page_number=1,
        lines=[
            OcrLine(
                text="图片中的求助提示",
                polygon_norm=[[0.1, 0.6], [0.9, 0.6], [0.9, 0.7], [0.1, 0.7]],
                confidence=0.92,
            )
        ],
        average_confidence=0.92,
        minimum_confidence=0.92,
    )
    page = PageEvidenceFusion().fuse(
        "docver_1",
        native_page(),
        PageRoute(text_strategy=TextStrategy.HYBRID, structure_strategy=StructureStrategy.LOCAL),
        ocr=ocr,
    )
    assert [block.text for block in page.blocks] == ["政策编号 12345", "图片中的求助提示"]
    assert page.blocks[1].provenance[0].provider == "paddleocr"


def test_vision_only_fallback_remains_searchable_when_ocr_is_disabled():
    source = PageEvidence(
        page_number=1,
        geometry=PageGeometry(width_px=1000, height_px=1400),
        image_path="page.png",
        native_blocks=[],
        features=PageFeatures(native_text_score=0.0),
    )
    vision = VisionPageResult(
        page_number=1,
        page_type="comic",
        blocks=[
            VisionBlock(
                type=BlockType.COMIC_PANEL,
                reading_order=0,
                bbox_norm=[0.1, 0.1, 0.9, 0.9],
                text="人物正在练习缓慢呼吸。",
                confidence=0.9,
            )
        ],
    )

    page = PageEvidenceFusion().fuse(
        "docver_1",
        source,
        PageRoute(text_strategy=TextStrategy.NATIVE, structure_strategy=StructureStrategy.VISION),
        vision=vision,
    )

    assert page.blocks[0].searchable is True
    assert page.blocks[0].text == "人物正在练习缓慢呼吸。"
    assert [item.provider for item in page.blocks[0].provenance] == ["vision"]
