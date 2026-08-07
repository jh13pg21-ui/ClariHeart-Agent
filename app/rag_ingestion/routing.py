from __future__ import annotations

from app.rag_ingestion.schema import (
    AccessClass,
    PageEvidence,
    PageRoute,
    PageStatus,
    StructureStrategy,
    TextStrategy,
)


class PageRouter:
    version = "page-router-v1"

    def __init__(self, *, ocr_enabled: bool = True, vision_enabled: bool = True):
        self.ocr_enabled = ocr_enabled
        self.vision_enabled = vision_enabled

    def route(
        self,
        page: PageEvidence,
        access_class: AccessClass,
        cloud_vision_allowed: bool,
    ) -> PageRoute:
        features = page.features
        reasons: list[str] = []

        force_vision = False
        if features.native_text_score < 0.65 and self.ocr_enabled:
            text_strategy = TextStrategy.PADDLE_OCR
            reasons.append("native_text_insufficient")
        elif features.native_text_score < 0.65:
            text_strategy = TextStrategy.NATIVE
            force_vision = True
            reasons.extend(["native_text_insufficient", "ocr_disabled"])
        elif features.image_text_risk >= 0.45 and self.ocr_enabled:
            text_strategy = TextStrategy.HYBRID
            reasons.append("image_text_risk")
        elif features.image_text_risk >= 0.45:
            text_strategy = TextStrategy.NATIVE
            force_vision = True
            reasons.extend(["image_text_risk", "ocr_disabled"])
        else:
            text_strategy = TextStrategy.NATIVE

        strong_visual = (
            features.multi_column
            or features.table_candidate
            or features.comic_candidate
            or features.infographic_candidate
        )
        if force_vision or strong_visual or features.layout_complexity >= 0.55 or features.visual_semantic_need >= 0.55:
            structure_strategy = StructureStrategy.VISION
            if features.multi_column:
                reasons.append("multi_column")
            if features.table_candidate:
                reasons.append("table_candidate")
            if features.comic_candidate:
                reasons.append("comic_candidate")
            if features.infographic_candidate:
                reasons.append("infographic_candidate")
            if not strong_visual:
                reasons.append("visual_complexity")
        elif features.image_text_risk >= 0.55:
            structure_strategy = StructureStrategy.VISION
            reasons.append("image_text_semantics")
        else:
            structure_strategy = StructureStrategy.LOCAL

        status = PageStatus.READY
        if structure_strategy == StructureStrategy.VISION:
            if not self.vision_enabled:
                status = PageStatus.NEEDS_REVIEW
                reasons.append("vision_disabled")
            elif not cloud_vision_allowed:
                status = PageStatus.NEEDS_REVIEW
                reasons.append("cloud_vision_forbidden")
        if access_class == AccessClass.RESTRICTED and structure_strategy == StructureStrategy.VISION:
            status = PageStatus.NEEDS_REVIEW
            if "cloud_vision_forbidden" not in reasons:
                reasons.append("cloud_vision_forbidden")

        return PageRoute(
            text_strategy=text_strategy,
            structure_strategy=structure_strategy,
            reasons=reasons,
            router_version=self.version,
            status=status,
        )

    def escalate_after_ocr(
        self,
        route: PageRoute,
        *,
        ocr_confidence: float,
        coverage_score: float,
        cloud_vision_allowed: bool,
    ) -> PageRoute:
        if ocr_confidence >= 0.82 and coverage_score >= 0.75:
            return route
        reasons = [*route.reasons, "ocr_quality_insufficient"]
        vision_available = self.vision_enabled and cloud_vision_allowed
        if not self.vision_enabled:
            reasons.append("vision_disabled")
        elif not cloud_vision_allowed:
            reasons.append("cloud_vision_forbidden")
        return route.model_copy(
            update={
                "structure_strategy": StructureStrategy.VISION,
                "status": PageStatus.READY if vision_available else PageStatus.NEEDS_REVIEW,
                "reasons": list(dict.fromkeys(reasons)),
            }
        )
