from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.rag_ingestion.errors import IngestionError, RetryableIngestionError
from app.rag_ingestion.schema import OcrLine, OcrPageEvidence


class PaddleOcrProvider:
    def __init__(
        self,
        *,
        device: str = "cpu",
        engine_factory: Callable[..., object] | None = None,
        image_size_reader: Callable[[Path], tuple[int, int]] | None = None,
    ):
        self.device = device
        self.engine_factory = engine_factory or self._default_engine_factory
        self.image_size_reader = image_size_reader or self._read_image_size
        self._engine = None

    def recognize(self, image_path: str | Path, *, page_number: int) -> OcrPageEvidence:
        path = Path(image_path)
        if not path.is_file():
            raise IngestionError("OCR 页面图片不存在", code="OCR_IMAGE_INVALID")
        started = time.perf_counter()
        width, height = self.image_size_reader(path)
        try:
            results = self._get_engine().predict(input=str(path))
        except IngestionError:
            raise
        except Exception as exc:
            raise RetryableIngestionError(f"PaddleOCR 运行失败: {type(exc).__name__}", code="OCR_RUNTIME_FAILED") from exc
        lines = self._lines(results, width, height)
        confidences = [line.confidence for line in lines]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return OcrPageEvidence(
            page_number=page_number,
            lines=lines,
            average_confidence=round(sum(confidences) / len(confidences), 6) if confidences else 0.0,
            minimum_confidence=min(confidences) if confidences else 0.0,
            provider_version=self._version(),
            model_name="PP-OCRv6",
            device=self.device,
            elapsed_ms=elapsed_ms,
        )

    def _get_engine(self):
        if self._engine is None:
            try:
                self._engine = self.engine_factory(
                    device=self.device,
                    lang="ch",
                    ocr_version="PP-OCRv6",
                    enable_mkldnn=False if self.device.startswith("cpu") else True,
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=True,
                )
            except Exception as exc:
                raise IngestionError(f"PaddleOCR 模型加载失败: {type(exc).__name__}", code="OCR_MODEL_LOAD_FAILED") from exc
        return self._engine

    @staticmethod
    def _default_engine_factory(**kwargs):
        from paddleocr import PaddleOCR

        return PaddleOCR(**kwargs)

    @staticmethod
    def _read_image_size(path: Path) -> tuple[int, int]:
        from PIL import Image

        with Image.open(path) as image:
            return image.size

    @classmethod
    def _lines(cls, results: Any, width: int, height: int) -> list[OcrLine]:
        lines: list[OcrLine] = []
        for result in results or []:
            data = cls._mapping(result)
            texts = data.get("rec_texts") or []
            scores = data.get("rec_scores") or []
            polygons = data.get("rec_polys") or data.get("dt_polys") or []
            for text, score, polygon in zip(texts, scores, polygons):
                if not str(text).strip():
                    continue
                normalized = [
                    [
                        max(0.0, min(1.0, float(point[0]) / max(1, width))),
                        max(0.0, min(1.0, float(point[1]) / max(1, height))),
                    ]
                    for point in polygon
                ]
                lines.append(OcrLine(text=str(text), polygon_norm=normalized, confidence=float(score)))
        return lines

    @staticmethod
    def _mapping(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result.get("res", result)
        json_value = getattr(result, "json", None)
        if callable(json_value):
            json_value = json_value()
        if isinstance(json_value, dict):
            return json_value.get("res", json_value)
        return {}

    @staticmethod
    def _version() -> str:
        try:
            return importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"
