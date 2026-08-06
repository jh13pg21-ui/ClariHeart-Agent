from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.rag_ingestion.schema import OcrPageEvidence


class OcrProvider(Protocol):
    def recognize(self, image_path: str | Path, *, page_number: int) -> OcrPageEvidence: ...
