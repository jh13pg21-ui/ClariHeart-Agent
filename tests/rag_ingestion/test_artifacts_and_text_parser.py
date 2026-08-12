from __future__ import annotations

import json

import pytest

from app.rag_ingestion.artifacts import ArtifactStore
from app.rag_ingestion.parsers.base import ParserContext
from app.rag_ingestion.parsers.text import TextDocumentParser
from app.rag_ingestion.schema import AccessClass, BlockType


def test_artifact_store_uses_only_system_ids_and_writes_valid_json_atomically(tmp_path):
    store = ArtifactStore(tmp_path)
    path = store.write_source("doc_abc", "a" * 64, b"payload")
    result = store.write_json("doc_abc", "a" * 64, "document.json", {"正文": "保留"})

    assert path.read_bytes() == b"payload"
    assert json.loads(result.read_text(encoding="utf-8")) == {"正文": "保留"}
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert not list(tmp_path.rglob("*.tmp"))


def test_artifact_store_rejects_path_traversal_components(tmp_path):
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="系统标识"):
        store.write_source("../outside", "a" * 64, b"payload")
    with pytest.raises(ValueError, match="artifact"):
        store.write_json("doc_abc", "a" * 64, "../secret.json", {})


def test_markdown_parser_preserves_heading_path_lists_and_code_blocks():
    parser = TextDocumentParser()
    parsed = parser.parse_bytes(
        "guide.md",
        "# 一级\n\n## 二级\n\n- 条目一\n- 条目二\n\n```python\nprint('保留换行')\n```".encode(),
        ParserContext(
            document_id="doc_1",
            version_id="docver_1",
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
        ),
    )

    blocks = parsed.pages[0].native_blocks
    assert [block.type for block in blocks] == [
        BlockType.HEADING,
        BlockType.HEADING,
        BlockType.LIST_ITEM,
        BlockType.LIST_ITEM,
        BlockType.CODE,
    ]
    assert blocks[-1].section_path == ["一级", "二级"]
    assert blocks[-1].text == "print('保留换行')"


def test_txt_parser_keeps_paragraph_boundaries_instead_of_collapsing_whitespace():
    parsed = TextDocumentParser().parse_bytes(
        "notes.txt",
        "第一段。\n仍是第一段。\n\n第二段。".encode(),
        ParserContext(
            document_id="doc_1",
            version_id="docver_1",
            access_class=AccessClass.ADMIN_PRIVATE,
            cloud_vision_allowed=False,
        ),
    )
    assert [block.text for block in parsed.pages[0].native_blocks] == ["第一段。\n仍是第一段。", "第二段。"]
