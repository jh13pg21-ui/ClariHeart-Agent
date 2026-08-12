from __future__ import annotations

from typing import Protocol

from app.rag_ingestion.schema import VisionAnalysisRequest, VisionPageResult


class VisionProvider(Protocol):
    def analyze(self, request: VisionAnalysisRequest) -> VisionPageResult: ...
