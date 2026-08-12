from __future__ import annotations

from pathlib import Path

from app.rag_ingestion.ocr.paddle import PaddleOcrProvider


class FakeEngine:
    def predict(self, input):
        assert Path(input).name == "page.png"
        return [
            {
                "rec_texts": ["第一行", "第二行"],
                "rec_scores": [0.95, 0.85],
                "rec_polys": [
                    [[10, 20], [90, 20], [90, 40], [10, 40]],
                    [[10, 50], [90, 50], [90, 70], [10, 70]],
                ],
            }
        ]


def test_paddle_engine_loads_once_and_normalizes_polygons(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fake")
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return FakeEngine()

    provider = PaddleOcrProvider(engine_factory=factory, image_size_reader=lambda _: (100, 200))
    first = provider.recognize(image, page_number=1)
    second = provider.recognize(image, page_number=1)

    assert len(calls) == 1
    assert calls[0]["enable_mkldnn"] is False
    assert first.lines[0].polygon_norm[0] == [0.1, 0.1]
    assert first.average_confidence == 0.9
    assert first.minimum_confidence == 0.85
    assert second.lines[1].text == "第二行"


def test_empty_paddle_result_has_zero_confidence(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(b"fake")
    provider = PaddleOcrProvider(
        engine_factory=lambda **_: type("Empty", (), {"predict": lambda self, input: []})(),
        image_size_reader=lambda _: (100, 200),
    )
    result = provider.recognize(image, page_number=2)
    assert result.lines == []
    assert result.average_confidence == 0.0
    assert result.minimum_confidence == 0.0
