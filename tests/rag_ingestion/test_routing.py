from __future__ import annotations

import pytest

from app.rag_ingestion.routing import PageRouter
from app.rag_ingestion.schema import (
    AccessClass,
    PageEvidence,
    PageFeatures,
    PageGeometry,
    PageStatus,
    StructureStrategy,
    TextStrategy,
)


def page(**features) -> PageEvidence:
    return PageEvidence(
        page_number=1,
        geometry=PageGeometry(width_px=1000, height_px=1400),
        image_path="page.png",
        features=PageFeatures(**features),
    )


@pytest.mark.parametrize(
    ("features", "text", "structure"),
    [
        ({"native_text_score": 0.92, "layout_complexity": 0.2}, TextStrategy.NATIVE, StructureStrategy.LOCAL),
        ({"native_text_score": 0.4, "layout_complexity": 0.2}, TextStrategy.PADDLE_OCR, StructureStrategy.LOCAL),
        ({"native_text_score": 0.9, "layout_complexity": 0.8, "multi_column": True}, TextStrategy.NATIVE, StructureStrategy.VISION),
        ({"native_text_score": 0.8, "image_text_risk": 0.8}, TextStrategy.HYBRID, StructureStrategy.VISION),
    ],
)
def test_text_and_structure_routes_are_independent(features, text, structure):
    route = PageRouter().route(page(**features), AccessClass.BUILTIN_PUBLIC, True)
    assert route.text_strategy == text
    assert route.structure_strategy == structure


def test_table_and_comic_signals_force_vision_even_with_good_native_text():
    router = PageRouter()
    assert router.route(page(native_text_score=0.95, table_candidate=True), AccessClass.BUILTIN_PUBLIC, True).structure_strategy == StructureStrategy.VISION
    assert router.route(page(native_text_score=0.95, comic_candidate=True), AccessClass.BUILTIN_PUBLIC, True).structure_strategy == StructureStrategy.VISION


def test_private_complex_page_requires_review_without_cloud_authorization():
    route = PageRouter().route(
        page(native_text_score=0.9, multi_column=True, layout_complexity=0.8),
        AccessClass.ADMIN_PRIVATE,
        False,
    )
    assert route.structure_strategy == StructureStrategy.VISION
    assert route.status == PageStatus.NEEDS_REVIEW
    assert "cloud_vision_forbidden" in route.reasons


def test_disabled_paddle_routes_weak_native_text_to_vision():
    route = PageRouter(ocr_enabled=False, vision_enabled=True).route(
        page(native_text_score=0.4, layout_complexity=0.2),
        AccessClass.BUILTIN_PUBLIC,
        True,
    )

    assert route.text_strategy == TextStrategy.NATIVE
    assert route.structure_strategy == StructureStrategy.VISION
    assert route.status == PageStatus.READY
    assert "ocr_disabled" in route.reasons


def test_disabled_vision_marks_complex_page_for_review():
    route = PageRouter(ocr_enabled=True, vision_enabled=False).route(
        page(native_text_score=0.95, table_candidate=True),
        AccessClass.BUILTIN_PUBLIC,
        True,
    )

    assert route.structure_strategy == StructureStrategy.VISION
    assert route.status == PageStatus.NEEDS_REVIEW
    assert "vision_disabled" in route.reasons


def test_disabled_ocr_and_vision_marks_weak_text_for_review():
    route = PageRouter(ocr_enabled=False, vision_enabled=False).route(
        page(native_text_score=0.3),
        AccessClass.BUILTIN_PUBLIC,
        True,
    )

    assert route.status == PageStatus.NEEDS_REVIEW
    assert "ocr_disabled" in route.reasons
    assert "vision_disabled" in route.reasons
