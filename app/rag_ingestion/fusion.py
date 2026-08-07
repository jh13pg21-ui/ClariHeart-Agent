from __future__ import annotations

from difflib import SequenceMatcher

from app.rag_ingestion.ids import stable_block_id
from app.rag_ingestion.schema import (
    BlockType,
    CanonicalBlock,
    CanonicalPage,
    EvidenceBlock,
    EvidenceProvenance,
    OcrLine,
    OcrPageEvidence,
    PageEvidence,
    PageQuality,
    PageRoute,
    StructureStrategy,
    TextStrategy,
    VisionBlock,
    VisionPageResult,
)


class PageEvidenceFusion:
    def fuse(
        self,
        version_id: str,
        page: PageEvidence,
        route: PageRoute,
        *,
        ocr: OcrPageEvidence | None = None,
        vision: VisionPageResult | None = None,
    ) -> CanonicalPage:
        if route.structure_strategy == StructureStrategy.VISION and vision is not None:
            blocks = self._vision_blocks(version_id, page, route, ocr, vision)
        else:
            blocks = self._local_blocks(version_id, page, route, ocr)
        blocks = [block.model_copy(update={"reading_order": index}) for index, block in enumerate(blocks)]
        final_score = sum(block.confidence for block in blocks) / len(blocks) if blocks else 0.0
        return CanonicalPage(
            page_number=page.page_number,
            geometry=page.geometry,
            route=route,
            quality=PageQuality(
                native_text_score=page.features.native_text_score,
                ocr_confidence=ocr.average_confidence if ocr else None,
                layout_complexity=page.features.layout_complexity,
                visual_semantic_need=page.features.visual_semantic_need,
                final_page_score=final_score,
            ),
            blocks=blocks,
        )

    def _local_blocks(
        self,
        version_id: str,
        page: PageEvidence,
        route: PageRoute,
        ocr: OcrPageEvidence | None,
    ) -> list[CanonicalBlock]:
        result: list[CanonicalBlock] = []
        if route.text_strategy != TextStrategy.PADDLE_OCR:
            result.extend(self._from_native(version_id, page, item, len(result)) for item in page.native_blocks)
        if ocr and route.text_strategy in {TextStrategy.PADDLE_OCR, TextStrategy.HYBRID}:
            for line in ocr.lines:
                bbox = self._ocr_bbox(line)
                if route.text_strategy == TextStrategy.HYBRID and any(self._iou(bbox, block.bbox_norm) >= 0.2 for block in result):
                    continue
                result.append(self._from_ocr(version_id, page.page_number, line, len(result)))
        return result

    def _vision_blocks(
        self,
        version_id: str,
        page: PageEvidence,
        route: PageRoute,
        ocr: OcrPageEvidence | None,
        vision: VisionPageResult,
    ) -> list[CanonicalBlock]:
        ordered = sorted(vision.blocks, key=lambda item: item.reading_order)
        result = []
        for index, visual in enumerate(ordered):
            native = self._best_native(visual, page.native_blocks)
            ocr_line = self._best_ocr(visual, ocr.lines if ocr else [])
            text, confidence, provenance = self._select_text(route.text_strategy, visual, native, ocr_line)
            searchable = visual.type not in {BlockType.HEADER, BlockType.FOOTER, BlockType.PAGE_NUMBER}
            if not provenance:
                searchable = False
            result.append(
                CanonicalBlock(
                    block_id=stable_block_id(version_id, page.page_number, index, text),
                    type=visual.type,
                    reading_order=index,
                    bbox_norm=visual.bbox_norm,
                    text=text,
                    section_path=visual.section_path,
                    confidence=confidence,
                    searchable=searchable,
                    provenance=provenance,
                    table=visual.table,
                    figure=visual.figure,
                )
            )
        return result

    def _select_text(
        self,
        strategy: TextStrategy,
        visual: VisionBlock,
        native: EvidenceBlock | None,
        ocr: OcrLine | None,
    ) -> tuple[str, float, list[EvidenceProvenance]]:
        provenance = [
            EvidenceProvenance(
                provider="vision",
                bbox_norm=visual.bbox_norm,
                confidence=visual.confidence,
                raw_text=visual.text,
            )
        ]
        if native is not None:
            provenance.insert(
                0,
                EvidenceProvenance(
                    provider="native",
                    source_block_id=native.evidence_id,
                    bbox_norm=native.bbox_norm,
                    confidence=native.confidence,
                    raw_text=native.text,
                ),
            )
        if ocr is not None:
            provenance.insert(
                1 if native is not None else 0,
                EvidenceProvenance(
                    provider="paddleocr",
                    bbox_norm=self._ocr_bbox(ocr),
                    confidence=ocr.confidence,
                    raw_text=ocr.text,
                ),
            )
        if strategy == TextStrategy.PADDLE_OCR and ocr is not None:
            return ocr.text, ocr.confidence, provenance
        if native is not None:
            return native.text, native.confidence, provenance
        if ocr is not None:
            return ocr.text, ocr.confidence, provenance
        return visual.text, min(visual.confidence, 0.5), provenance

    def _from_native(self, version_id: str, page: PageEvidence, item: EvidenceBlock, order: int) -> CanonicalBlock:
        return CanonicalBlock(
            block_id=stable_block_id(version_id, page.page_number, order, item.text),
            type=item.type,
            reading_order=order,
            bbox_norm=item.bbox_norm,
            text=item.text,
            section_path=item.section_path,
            confidence=item.confidence,
            provenance=[
                EvidenceProvenance(
                    provider="native",
                    source_block_id=item.evidence_id,
                    bbox_norm=item.bbox_norm,
                    confidence=item.confidence,
                )
            ],
        )

    def _from_ocr(self, version_id: str, page_number: int, line: OcrLine, order: int) -> CanonicalBlock:
        bbox = self._ocr_bbox(line)
        return CanonicalBlock(
            block_id=stable_block_id(version_id, page_number, order, line.text),
            type=BlockType.PARAGRAPH,
            reading_order=order,
            bbox_norm=bbox,
            text=line.text,
            confidence=line.confidence,
            provenance=[EvidenceProvenance(provider="paddleocr", bbox_norm=bbox, confidence=line.confidence)],
        )

    def _best_native(self, visual: VisionBlock, blocks: list[EvidenceBlock]) -> EvidenceBlock | None:
        return self._best_match(visual.bbox_norm, visual.text, blocks, lambda item: item.bbox_norm, lambda item: item.text)

    def _best_ocr(self, visual: VisionBlock, lines: list[OcrLine]) -> OcrLine | None:
        return self._best_match(visual.bbox_norm, visual.text, lines, self._ocr_bbox, lambda item: item.text)

    def _best_match(self, bbox, text, items, bbox_of, text_of):
        scored = []
        for item in items:
            overlap = self._iou(bbox, bbox_of(item))
            similarity = SequenceMatcher(None, text, text_of(item)).ratio()
            score = overlap * 0.75 + similarity * 0.25
            if overlap >= 0.15 or score >= 0.35:
                scored.append((score, item))
        return max(scored, key=lambda pair: pair[0])[1] if scored else None

    @staticmethod
    def _ocr_bbox(line: OcrLine) -> list[float]:
        xs = [point[0] for point in line.polygon_norm]
        ys = [point[1] for point in line.polygon_norm]
        return [min(xs), min(ys), max(xs), max(ys)]

    @staticmethod
    def _iou(left: list[float], right: list[float]) -> float:
        x0, y0 = max(left[0], right[0]), max(left[1], right[1])
        x1, y1 = min(left[2], right[2]), min(left[3], right[3])
        intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        left_area = (left[2] - left[0]) * (left[3] - left[1])
        right_area = (right[2] - right[0]) * (right[3] - right[1])
        union = left_area + right_area - intersection
        return intersection / union if union > 0 else 0.0
