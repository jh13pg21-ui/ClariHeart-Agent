from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.rag_ingestion.ids import stable_chunk_id
from app.rag_ingestion.schema import (
    BlockType,
    CanonicalBlock,
    CanonicalDocument,
    ChunkDraft,
    ChunkKind,
    TableData,
)


@dataclass(frozen=True)
class ChunkingConfig:
    child_target_tokens: int = 400
    child_min_tokens: int = 120
    child_max_tokens: int = 650
    child_overlap_tokens: int = 60
    parent_target_tokens: int = 1200
    parent_max_tokens: int = 1800


@dataclass(frozen=True)
class _BlockRef:
    page_number: int
    block: CanonicalBlock


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    other = len(re.findall(r"[^\s\u3400-\u9fffA-Za-z0-9_]", text))
    return chinese + words + (other + 3) // 4


class StructureAwareChunker:
    def __init__(self, config: ChunkingConfig | None = None):
        self.config = config or ChunkingConfig()

    def chunk(self, document: CanonicalDocument) -> list[ChunkDraft]:
        groups = self._groups(document)
        drafts: list[ChunkDraft] = []
        source_index = 0
        for refs in groups:
            parent_content = self._parent_content(refs)
            block_ids = [ref.block.block_id for ref in refs]
            page_start = min(ref.page_number for ref in refs)
            page_end = max(ref.page_number for ref in refs)
            section_path = refs[0].block.section_path
            parent_id = stable_chunk_id(
                document.document_id,
                document.version_id,
                ChunkKind.PARENT.value,
                block_ids,
                parent_content,
            )
            drafts.append(
                self._draft(
                    document,
                    parent_id,
                    None,
                    ChunkKind.PARENT,
                    source_index,
                    parent_content,
                    page_start,
                    page_end,
                    section_path,
                    block_ids,
                )
            )
            source_index += 1
            for content, child_refs in self._child_contents(refs):
                child_block_ids = list(dict.fromkeys(ref.block.block_id for ref in child_refs))
                child_page_start = min(ref.page_number for ref in child_refs)
                child_page_end = max(ref.page_number for ref in child_refs)
                child_id = stable_chunk_id(
                    document.document_id,
                    document.version_id,
                    ChunkKind.CHILD.value,
                    child_block_ids,
                    content,
                )
                drafts.append(
                    self._draft(
                        document,
                        child_id,
                        parent_id,
                        ChunkKind.CHILD,
                        source_index,
                        content,
                        child_page_start,
                        child_page_end,
                        child_refs[0].block.section_path,
                        child_block_ids,
                    )
                )
                source_index += 1
        return drafts

    def _groups(self, document: CanonicalDocument) -> list[list[_BlockRef]]:
        groups: list[list[_BlockRef]] = []
        current: list[_BlockRef] = []
        current_key: tuple[str, ...] | None = None
        parent_target = max(1, min(self.config.parent_target_tokens, self.config.parent_max_tokens))
        special = {BlockType.TABLE, BlockType.FIGURE, BlockType.COMIC_PANEL}
        for page in document.pages:
            for block in sorted(page.blocks, key=lambda item: item.reading_order):
                if not block.searchable or not block.text.strip():
                    continue
                ref = _BlockRef(page.page_number, block)
                key = tuple(block.section_path)
                if block.type in special:
                    if current:
                        groups.append(current)
                        current = []
                    groups.append([ref])
                    current_key = None
                    continue
                projected = self._parent_content([*current, ref]) if current else block.text
                if current and (
                    key != current_key
                    or estimate_tokens(projected) > parent_target
                    or estimate_tokens(projected) > self.config.parent_max_tokens
                ):
                    groups.append(current)
                    current = []
                current.append(ref)
                current_key = key
        if current:
            groups.append(current)
        return groups

    def _parent_content(self, refs: list[_BlockRef]) -> str:
        prefix = self._prefix(refs)
        body = "\n\n".join(ref.block.text.strip() for ref in refs if ref.block.text.strip())
        return f"{prefix}\n{body}".strip()

    def _child_contents(self, refs: list[_BlockRef]) -> list[tuple[str, list[_BlockRef]]]:
        if len(refs) == 1 and refs[0].block.type == BlockType.TABLE and refs[0].block.table:
            return [(content, refs) for content in self._table_chunks(refs[0])]
        if len(refs) == 1 and refs[0].block.type in {BlockType.FIGURE, BlockType.COMIC_PANEL}:
            return [(self._parent_content(refs), refs)]

        prefix = self._prefix(refs)
        prefix_tokens = estimate_tokens(prefix)
        max_body_tokens = max(1, self.config.child_max_tokens - prefix_tokens)
        target_body_tokens = max(
            1,
            min(max_body_tokens, self.config.child_target_tokens - prefix_tokens),
        )
        units: list[tuple[str, _BlockRef]] = []
        for ref in refs:
            for piece in self._split_text(ref.block.text, max_body_tokens):
                units.append((piece, ref))
        groups = self._pack_child_units(units, target_body_tokens)
        groups = self._merge_small_child_groups(prefix, groups)

        chunks: list[tuple[str, list[_BlockRef]]] = []
        for index, group in enumerate(groups):
            rendered = self._render_child(prefix, group)
            overlap: list[tuple[str, _BlockRef]] = []
            if index > 0 and self.config.child_overlap_tokens > 0:
                available = max(0, self.config.child_max_tokens - estimate_tokens(rendered))
                overlap = self._overlap_units(
                    groups[index - 1],
                    min(self.config.child_overlap_tokens, available),
                )
            final_units = [*overlap, *group]
            chunks.append(
                (
                    self._render_child(prefix, final_units),
                    self._unique_refs(ref for _, ref in final_units),
                )
            )
        return chunks

    @staticmethod
    def _unique_refs(refs) -> list[_BlockRef]:
        result: list[_BlockRef] = []
        seen: set[tuple[int, str]] = set()
        for ref in refs:
            key = (ref.page_number, ref.block.block_id)
            if key not in seen:
                seen.add(key)
                result.append(ref)
        return result

    @staticmethod
    def _pack_child_units(
        units: list[tuple[str, _BlockRef]],
        target_body_tokens: int,
    ) -> list[list[tuple[str, _BlockRef]]]:
        groups: list[list[tuple[str, _BlockRef]]] = []
        current: list[tuple[str, _BlockRef]] = []
        for unit in units:
            projected = "\n".join(text for text, _ in [*current, unit])
            if current and estimate_tokens(projected) > target_body_tokens:
                groups.append(current)
                current = []
            current.append(unit)
        if current:
            groups.append(current)
        return groups

    def _merge_small_child_groups(
        self,
        prefix: str,
        groups: list[list[tuple[str, _BlockRef]]],
    ) -> list[list[tuple[str, _BlockRef]]]:
        minimum = min(self.config.child_min_tokens, self.config.child_max_tokens)
        index = 0
        while len(groups) > 1 and index < len(groups):
            if estimate_tokens(self._render_child(prefix, groups[index])) >= minimum:
                index += 1
                continue
            if index > 0:
                merged = [*groups[index - 1], *groups[index]]
                if estimate_tokens(self._render_child(prefix, merged)) <= self.config.child_max_tokens:
                    groups[index - 1] = merged
                    groups.pop(index)
                    index = max(0, index - 1)
                    continue
            if index + 1 < len(groups):
                merged = [*groups[index], *groups[index + 1]]
                if estimate_tokens(self._render_child(prefix, merged)) <= self.config.child_max_tokens:
                    groups[index] = merged
                    groups.pop(index + 1)
                    continue
            index += 1
        return groups

    @staticmethod
    def _render_child(prefix: str, units: list[tuple[str, _BlockRef]]) -> str:
        return f"{prefix}\n" + "\n".join(text for text, _ in units)

    @classmethod
    def _overlap_units(
        cls,
        previous: list[tuple[str, _BlockRef]],
        budget: int,
    ) -> list[tuple[str, _BlockRef]]:
        if budget <= 0:
            return []
        selected: list[tuple[str, _BlockRef]] = []
        remaining = budget
        for text, ref in reversed(previous):
            clean = text.strip()
            tokens = estimate_tokens(clean)
            if tokens <= remaining:
                selected.insert(0, (clean, ref))
                remaining -= tokens
                continue
            tail = cls._tail_within_budget(clean, remaining)
            if tail:
                selected.insert(0, (tail, ref))
            break
        return selected

    @staticmethod
    def _tail_within_budget(text: str, budget: int) -> str:
        if budget <= 0:
            return ""
        candidate = ""
        for character in reversed(text.strip()):
            projected = character + candidate
            if estimate_tokens(projected) > budget:
                break
            candidate = projected
        return candidate.strip()

    def _table_chunks(self, ref: _BlockRef) -> list[str]:
        table = ref.block.table
        assert table is not None
        rows = self._render_table_rows(table)
        if not rows:
            return [self._parent_content([ref])]
        header = rows[0]
        prefix = self._prefix([ref])
        fixed_tokens = estimate_tokens(prefix) + estimate_tokens(header) + 2
        budget = max(1, self.config.child_max_tokens - fixed_tokens)
        chunks: list[str] = []
        current: list[str] = []
        for row in rows[1:]:
            projected = "\n".join([*current, row])
            if current and estimate_tokens(projected) > budget:
                chunks.append(f"{prefix}\n{header}\n" + "\n".join(current))
                current.clear()
            current.append(row)
        if current:
            chunks.append(f"{prefix}\n{header}\n" + "\n".join(current))
        return chunks or [f"{prefix}\n{header}"]

    @staticmethod
    def _render_table_rows(table: TableData) -> list[str]:
        matrix = [["" for _ in range(table.columns)] for _ in range(table.rows)]
        for cell in table.cells:
            if cell.row < table.rows and cell.column < table.columns:
                matrix[cell.row][cell.column] = cell.text.strip()
        return [" | ".join(row) for row in matrix if any(value for value in row)]

    def _split_text(self, text: str, budget: int) -> list[str]:
        clean = text.strip()
        if estimate_tokens(clean) <= budget:
            return [clean]
        sentences = [item for item in re.split(r"(?<=[。！？；!?;])", clean) if item]
        pieces: list[str] = []
        current = ""
        for sentence in sentences:
            if estimate_tokens(sentence) > budget:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(self._hard_split(sentence, budget))
                continue
            if current and estimate_tokens(current + sentence) > budget:
                pieces.append(current)
                current = sentence
            else:
                current += sentence
        if current:
            pieces.append(current)
        return pieces

    @staticmethod
    def _hard_split(text: str, budget: int) -> list[str]:
        pieces: list[str] = []
        current = ""
        for character in text:
            if current and estimate_tokens(current + character) > budget:
                pieces.append(current)
                current = character
            else:
                current += character
        if current:
            pieces.append(current)
        return pieces

    @staticmethod
    def _prefix(refs: list[_BlockRef]) -> str:
        pages = sorted({ref.page_number for ref in refs})
        page_label = str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"
        section = " > ".join(refs[0].block.section_path) or "未命名章节"
        return f"[章节] {section}\n[页码] {page_label}"

    @staticmethod
    def _draft(
        document: CanonicalDocument,
        stable_id: str,
        parent_stable_id: str | None,
        kind: ChunkKind,
        source_index: int,
        content: str,
        page_start: int,
        page_end: int,
        section_path: list[str],
        block_ids: list[str],
    ) -> ChunkDraft:
        return ChunkDraft(
            stable_id=stable_id,
            document_id=document.document_id,
            document_version_id=document.version_id,
            parent_stable_id=parent_stable_id,
            chunk_kind=kind,
            source=document.source.filename,
            source_index=source_index,
            content=content,
            page_start=page_start,
            page_end=page_end,
            section_path=section_path,
            block_ids=block_ids,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
