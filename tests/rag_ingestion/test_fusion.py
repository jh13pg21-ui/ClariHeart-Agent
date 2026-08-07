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


def test_local_fusion_excludes_browser_print_chrome_from_searchable_content():
    source = PageEvidence(
        page_number=2,
        geometry=PageGeometry(width_px=1240, height_px=1754),
        image_path="page.png",
        native_blocks=[
            EvidenceBlock(
                evidence_id="timestamp",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.04, 0.018, 0.13, 0.029],
                text="2026/8/6 21:44",
            ),
            EvidenceBlock(
                evidence_id="browser-title",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.34, 0.019, 0.80, 0.031],
                text="关于印发实施方案的通知",
            ),
            EvidenceBlock(
                evidence_id="body",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.12, 0.18, 0.89, 0.22],
                text="健全社会心理服务体系和危机干预机制实施方案",
            ),
            EvidenceBlock(
                evidence_id="url",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.04, 0.972, 0.80, 0.983],
                text="https://example.com/policy",
            ),
            EvidenceBlock(
                evidence_id="page-number",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.93, 0.972, 0.96, 0.983],
                text="2/7",
            ),
        ],
        features=PageFeatures(native_text_score=1.0),
    )

    page = PageEvidenceFusion().fuse(
        "docver_1",
        source,
        PageRoute(text_strategy=TextStrategy.NATIVE, structure_strategy=StructureStrategy.LOCAL),
    )

    searchable = [block.text for block in page.blocks if block.searchable]
    assert searchable == ["健全社会心理服务体系和危机干预机制实施方案"]
    assert [block.type for block in page.blocks] == [
        BlockType.HEADER,
        BlockType.HEADER,
        BlockType.PARAGRAPH,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
    ]


def test_local_fusion_respects_explicit_non_content_block_types():
    source = PageEvidence(
        page_number=1,
        geometry=PageGeometry(width_px=1000, height_px=1400),
        image_path="page.png",
        native_blocks=[
            EvidenceBlock(
                evidence_id="header",
                type=BlockType.HEADER,
                bbox_norm=[0.1, 0.1, 0.9, 0.2],
                text="重复页眉",
            ),
            EvidenceBlock(
                evidence_id="body",
                type=BlockType.PARAGRAPH,
                bbox_norm=[0.1, 0.3, 0.9, 0.4],
                text="正文",
            ),
        ],
        features=PageFeatures(native_text_score=1.0),
    )

    page = PageEvidenceFusion().fuse(
        "docver_1",
        source,
        PageRoute(text_strategy=TextStrategy.NATIVE, structure_strategy=StructureStrategy.LOCAL),
    )

    assert [block.searchable for block in page.blocks] == [False, True]
