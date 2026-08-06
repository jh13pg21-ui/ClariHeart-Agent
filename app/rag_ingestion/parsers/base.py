from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.rag_ingestion.schema import AccessClass, ParseEvidence


@dataclass(frozen=True)
class ParserContext:
    document_id: str
    version_id: str
    access_class: AccessClass
    cloud_vision_allowed: bool
    source_sha256: str = ""


class DocumentParser(Protocol):
    def parse_bytes(self, filename: str, data: bytes, context: ParserContext) -> ParseEvidence: ...
