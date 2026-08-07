from __future__ import annotations

from types import SimpleNamespace

from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.parsers.base import ParserContext
from app.rag_ingestion.parsers.liteparse import LiteParseDocumentParser
from app.rag_ingestion.schema import AccessClass


class FakeLiteParse:
    kwargs = None
    screenshot_path = None

    def __init__(self, **kwargs):
        type(self).kwargs = kwargs

    def parse(self, data):
        text_item = SimpleNamespace(
            text="政策正文",
            x=10.0,
            y=20.0,
            width=80.0,
            height=10.0,
            confidence=None,
        )
        complexity = SimpleNamespace(
            text_length=4,
            text_coverage=0.2,
            image_coverage=0.1,
            needs_ocr=False,
            reasons=[],
            is_garbled=False,
            layout=SimpleNamespace(
                column_count=1,
                ruled_table_count=0,
                figure_count=0,
                figure_coverage=0.0,
                is_complex=False,
                reasons=[],
            ),
        )
        page = SimpleNamespace(
            page_num=1,
            width=100.0,
            height=200.0,
            text="政策正文",
            text_items=[text_item],
            complexity=complexity,
            vector_graphics=SimpleNamespace(lines=[], shapes=[]),
        )
        return SimpleNamespace(pages=[page])

    def screenshot(self, path, page_numbers=None):
        type(self).screenshot_path = path
        return [SimpleNamespace(page_num=1, width=1000, height=2000, image_bytes=b"png")]


def test_liteparse_adapter_disables_ocr_and_normalizes_page_evidence(tmp_path):
    parser = LiteParseDocumentParser(
        artifact_store=ArtifactStore(tmp_path),
        parser_factory=FakeLiteParse,
        dpi=150,
    )
    result = parser.parse_bytes(
        "policy.pdf",
        b"%PDF-fake",
        ParserContext(
            document_id="doc_1",
            version_id="docver_1",
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
            source_sha256="b" * 64,
        ),
    )

    assert FakeLiteParse.kwargs["ocr_enabled"] is False
    assert FakeLiteParse.kwargs["include_complexity"] is True
    assert FakeLiteParse.screenshot_path.suffix == ".pdf"
    assert result.pages[0].geometry.width_px == 1000
    assert result.pages[0].native_blocks[0].bbox_norm == [0.1, 0.1, 0.9, 0.15]
    assert result.pages[0].native_blocks[0].confidence == 1.0
    assert result.pages[0].features.native_text_score > 0
    assert (tmp_path / "doc_1" / ("b" * 64) / "pages" / "0001.png").read_bytes() == b"png"


def test_liteparse_adapter_forwards_explicit_ocr_switch(tmp_path):
    parser = LiteParseDocumentParser(
        artifact_store=ArtifactStore(tmp_path),
        parser_factory=FakeLiteParse,
        ocr_enabled=True,
    )

    parser.parse_bytes(
        "scan.pdf",
        b"%PDF-fake",
        ParserContext(
            document_id="doc_2",
            version_id="docver_2",
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
            source_sha256="c" * 64,
        ),
    )

    assert FakeLiteParse.kwargs["ocr_enabled"] is True
