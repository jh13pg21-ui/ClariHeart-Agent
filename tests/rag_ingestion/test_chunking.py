from __future__ import annotations

from app.rag_ingestion.chunking import ChunkingConfig, StructureAwareChunker, estimate_tokens
from app.rag_ingestion.schema import (
    AccessClass,
    BlockType,
    CanonicalBlock,
    CanonicalDocument,
    CanonicalPage,
    ChunkKind,
    EvidenceProvenance,
    PageGeometry,
    PageQuality,
    PageRoute,
    ParserRunInfo,
    SourceInfo,
    StructureStrategy,
    TableCell,
    TableData,
    TextStrategy,
)


def block(block_id, order, text, *, block_type=BlockType.PARAGRAPH, table=None, searchable=True):
    return CanonicalBlock(
        block_id=block_id,
        type=block_type,
        reading_order=order,
        bbox_norm=[0.1, 0.1 + order * 0.1, 0.9, 0.18 + order * 0.1],
        text=text,
        section_path=["压力管理", "日常练习"],
        searchable=searchable,
        confidence=0.95,
        provenance=[EvidenceProvenance(provider="native")],
        table=table,
    )


def document(blocks):
    return CanonicalDocument(
        document_id="doc_1",
        version_id="docver_1",
        source=SourceInfo(
            filename="guide.pdf",
            mime_type="application/pdf",
            sha256="a" * 64,
            size_bytes=100,
            access_class=AccessClass.BUILTIN_PUBLIC,
            cloud_vision_allowed=True,
        ),
        parser_run=ParserRunInfo(),
        pages=[
            CanonicalPage(
                page_number=1,
                geometry=PageGeometry(width_px=1000, height_px=1400),
                route=PageRoute(text_strategy=TextStrategy.NATIVE, structure_strategy=StructureStrategy.LOCAL),
                quality=PageQuality(final_page_score=0.95),
                blocks=blocks,
            )
        ],
    )


def test_chunker_builds_stable_parent_child_relationships_and_skips_headers():
    doc = document(
        [
            block("h", 0, "重复页眉", block_type=BlockType.HEADER, searchable=False),
            block("b1", 1, "先识别压力信号。记录身体和情绪变化。"),
            block("b2", 2, "再进行缓慢呼吸。把注意力放回当下。"),
        ]
    )
    chunker = StructureAwareChunker(ChunkingConfig(child_target_tokens=20, child_min_tokens=5, child_max_tokens=30))
    first = chunker.chunk(doc)
    second = chunker.chunk(doc)

    parents = [item for item in first if item.chunk_kind == ChunkKind.PARENT]
    children = [item for item in first if item.chunk_kind == ChunkKind.CHILD]
    assert len(parents) == 1
    assert children
    assert all(item.parent_stable_id == parents[0].stable_id for item in children)
    assert all(item.page_start == 1 and item.page_end == 1 for item in first)
    assert all("重复页眉" not in item.content for item in first)
    assert [item.stable_id for item in first] == [item.stable_id for item in second]


def test_long_table_splits_by_complete_rows_and_repeats_header():
    cells = [TableCell(row=0, column=0, text="姓名"), TableCell(row=0, column=1, text="处理方式")]
    for row in range(1, 41):
        cells.extend(
            [
                TableCell(row=row, column=0, text=f"对象{row}"),
                TableCell(row=row, column=1, text=f"完整处置步骤{row}"),
            ]
        )
    table = TableData(rows=41, columns=2, cells=cells)
    doc = document([block("table-1", 0, "处置表", block_type=BlockType.TABLE, table=table)])
    chunks = StructureAwareChunker(
        ChunkingConfig(child_target_tokens=45, child_min_tokens=10, child_max_tokens=60)
    ).chunk(doc)
    children = [item for item in chunks if item.chunk_kind == ChunkKind.CHILD]

    assert len(children) > 1
    assert all("姓名 | 处理方式" in item.content for item in children)
    assert all("对象" in item.content for item in children)
    assert all(item.block_ids == ["table-1"] for item in children)
    assert all(estimate_tokens(item.content) <= 75 for item in children)


def test_sentence_split_respects_chinese_boundaries_and_maximum():
    text = "这是一个用于测试的完整句子。" * 30
    doc = document([block("long", 0, text)])
    children = [
        item
        for item in StructureAwareChunker(
            ChunkingConfig(child_target_tokens=40, child_min_tokens=10, child_max_tokens=50)
        ).chunk(doc)
        if item.chunk_kind == ChunkKind.CHILD
    ]
    assert len(children) > 1
    assert all(item.content.rstrip().endswith("。") for item in children)
    assert all(estimate_tokens(item.content) <= 60 for item in children)
