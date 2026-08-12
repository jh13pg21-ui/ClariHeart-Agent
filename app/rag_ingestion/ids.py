from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def stable_digest(prefix: str, payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def stable_document_id(source_key: str) -> str:
    return stable_digest("doc", {"source_key": source_key.strip().lower()})


def stable_version_id(document_id: str, sha256: str, pipeline_fingerprint: str) -> str:
    return stable_digest(
        "docver",
        {"document_id": document_id, "sha256": sha256, "pipeline": pipeline_fingerprint},
    )


def stable_block_id(version_id: str, page_number: int, reading_order: int, text: str) -> str:
    return stable_digest(
        "block",
        {
            "version_id": version_id,
            "page_number": page_number,
            "reading_order": reading_order,
            "text": normalized_text(text),
        },
    )


def stable_chunk_id(
    document_id: str,
    version_id: str,
    chunk_kind: str,
    block_ids: Sequence[str],
    content: str,
) -> str:
    return stable_digest(
        "chunk",
        {
            "document_id": document_id,
            "version_id": version_id,
            "chunk_kind": chunk_kind,
            "block_ids": list(block_ids),
            "content": normalized_text(content),
        },
    )
