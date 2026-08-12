from __future__ import annotations

import mimetypes
import re

from app.rag_ingestion.ids import stable_block_id
from app.rag_ingestion.parsers.base import ParserContext
from app.rag_ingestion.schema import (
    BlockType,
    EvidenceBlock,
    PageEvidence,
    PageFeatures,
    PageGeometry,
    ParseEvidence,
)


class TextDocumentParser:
    def parse_bytes(self, filename: str, data: bytes, context: ParserContext) -> ParseEvidence:
        text = data.decode("utf-8-sig", errors="replace")
        blocks = self._markdown_blocks(text, context.version_id) if filename.lower().endswith(".md") else self._text_blocks(text, context.version_id)
        character_count = sum(len(block.text) for block in blocks)
        page = PageEvidence(
            page_number=1,
            geometry=PageGeometry(width_px=1000, height_px=max(1400, 80 * max(1, len(blocks)))),
            image_path="",
            native_blocks=blocks,
            features=PageFeatures(
                native_character_count=character_count,
                native_text_score=1.0 if character_count else 0.0,
            ),
        )
        mime_type = mimetypes.guess_type(filename)[0] or "text/plain"
        return ParseEvidence(
            filename=filename,
            mime_type=mime_type,
            parser_name="structured_text",
            parser_version="1.0",
            pages=[page],
        )

    def _markdown_blocks(self, text: str, version_id: str) -> list[EvidenceBlock]:
        blocks: list[tuple[BlockType, str, list[str]]] = []
        section_path: list[str] = []
        paragraph: list[str] = []
        code_lines: list[str] = []
        in_code = False

        def flush_paragraph() -> None:
            if paragraph:
                blocks.append((BlockType.PARAGRAPH, "\n".join(paragraph).strip(), section_path.copy()))
                paragraph.clear()

        for line in text.splitlines():
            if line.startswith("```"):
                if in_code:
                    blocks.append((BlockType.CODE, "\n".join(code_lines), section_path.copy()))
                    code_lines.clear()
                    in_code = False
                else:
                    flush_paragraph()
                    in_code = True
                continue
            if in_code:
                code_lines.append(line)
                continue
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                title = heading.group(2)
                section_path = section_path[: level - 1] + [title]
                blocks.append((BlockType.HEADING, title, section_path.copy()))
                continue
            list_item = re.match(r"^\s*(?:[-*+] |\d+[.)]\s+)(.+)$", line)
            if list_item:
                flush_paragraph()
                blocks.append((BlockType.LIST_ITEM, list_item.group(1).strip(), section_path.copy()))
                continue
            if not line.strip():
                flush_paragraph()
            else:
                paragraph.append(line.rstrip())
        flush_paragraph()
        if in_code:
            blocks.append((BlockType.CODE, "\n".join(code_lines), section_path.copy()))
        return self._to_evidence(blocks, version_id)

    def _text_blocks(self, text: str, version_id: str) -> list[EvidenceBlock]:
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
        return self._to_evidence(
            [(BlockType.PARAGRAPH, paragraph, []) for paragraph in paragraphs],
            version_id,
        )

    def _to_evidence(
        self,
        blocks: list[tuple[BlockType, str, list[str]]],
        version_id: str,
    ) -> list[EvidenceBlock]:
        count = max(1, len(blocks))
        result = []
        for index, (block_type, text, section_path) in enumerate(blocks):
            y0 = index / count
            y1 = min(1.0, (index + 1) / count)
            result.append(
                EvidenceBlock(
                    evidence_id=stable_block_id(version_id, 1, index, text),
                    type=block_type,
                    bbox_norm=[0.05, y0, 0.95, y1],
                    text=text,
                    confidence=1.0,
                    section_path=section_path,
                )
            )
        return result
