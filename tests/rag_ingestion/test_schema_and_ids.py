from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.rag_ingestion.ids import file_sha256, stable_block_id, stable_chunk_id
from app.rag_ingestion.schema import (
    AccessClass,
    BlockType,
    CanonicalBlock,
    CanonicalPage,
    EvidenceProvenance,
    PageGeometry,
    PageQuality,
    PageRoute,
    StructureStrategy,
    TextStrategy,
    VisionPageResult,
)


def make_block(order: int, bbox: list[float] | None = None) -> CanonicalBlock:
    return CanonicalBlock(
        block_id=f"block-{order}",
        type=BlockType.PARAGRAPH,
        reading_order=order,
        bbox_norm=bbox or [0.1, 0.1, 0.9, 0.2],
        text="正文",
        confidence=0.9,
        provenance=[EvidenceProvenance(provider="native", confidence=0.9)],
    )


def test_block_rejects_bbox_outside_normalized_page():
    with pytest.raises(ValidationError, match="bbox_norm"):
        make_block(0, [0.0, 0.0, 1.2, 1.0])


def test_page_rejects_duplicate_reading_order():
    with pytest.raises(ValidationError, match="reading_order"):
        CanonicalPage(
            page_number=1,
            geometry=PageGeometry(width_px=1000, height_px=1400),
            route=PageRoute(
                text_strategy=TextStrategy.NATIVE,
                structure_strategy=StructureStrategy.LOCAL,
            ),
            quality=PageQuality(native_text_score=0.9, final_page_score=0.9),
            blocks=[make_block(0), make_block(0)],
        )


def test_vision_result_rejects_translated_or_unknown_protocol_fields():
    with pytest.raises(ValidationError):
        VisionPageResult.model_validate(
            {
                "page_number": 1,
                "blocks": [],
                "页面类型": "政策通知",
            }
        )


def test_stable_ids_are_repeatable_and_content_sensitive():
    first = stable_chunk_id("doc1", "v1", "child", ["b1"], "心理支持")
    assert first == stable_chunk_id("doc1", "v1", "child", ["b1"], "心理支持")
    assert first != stable_chunk_id("doc1", "v1", "child", ["b1"], "危机干预")
    assert stable_block_id("v1", 1, 0, "正文") == stable_block_id("v1", 1, 0, "正文")
    assert file_sha256(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_access_class_values_are_protocol_stable():
    assert AccessClass.BUILTIN_PUBLIC.value == "BUILTIN_PUBLIC"
    assert AccessClass.ADMIN_PRIVATE.value == "ADMIN_PRIVATE"
    assert AccessClass.RESTRICTED.value == "RESTRICTED"
