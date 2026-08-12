from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel


_SYSTEM_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def version_dir(self, document_id: str, version_sha256: str) -> Path:
        self._validate_system_id(document_id)
        if not _SHA256.fullmatch(version_sha256):
            raise ValueError("版本哈希必须是 64 位小写 SHA-256")
        destination = self.root / document_id / version_sha256
        self._assert_within_root(destination)
        return destination

    def write_source(self, document_id: str, version_sha256: str, data: bytes) -> Path:
        destination = self.version_dir(document_id, version_sha256) / "source.bin"
        return self._atomic_write(destination, data)

    def write_json(
        self,
        document_id: str,
        version_sha256: str,
        artifact_name: str,
        value: Any,
    ) -> Path:
        relative = Path(artifact_name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("artifact 名称必须是版本目录内的安全相对路径")
        if any(not _SYSTEM_ID.fullmatch(part.replace(".", "_")) for part in relative.parts):
            raise ValueError("artifact 名称包含非法字符")
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return self._atomic_write(self.version_dir(document_id, version_sha256) / relative, encoded)

    def read_json(self, document_id: str, version_sha256: str, artifact_name: str) -> Any:
        path = self.version_dir(document_id, version_sha256) / artifact_name
        self._assert_within_root(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def write_page_bytes(
        self,
        document_id: str,
        version_sha256: str,
        page_number: int,
        data: bytes,
        suffix: str = ".png",
    ) -> Path:
        if page_number < 1 or suffix not in {".png", ".json"}:
            raise ValueError("页码或 artifact 后缀非法")
        destination = self.version_dir(document_id, version_sha256) / "pages" / f"{page_number:04d}{suffix}"
        return self._atomic_write(destination, data)

    def page_path(self, document_id: str, version_sha256: str, page_number: int) -> Path:
        return self.version_dir(document_id, version_sha256) / "pages" / f"{page_number:04d}.png"

    @contextmanager
    def parser_input(
        self,
        document_id: str,
        version_sha256: str,
        data: bytes,
        suffix: str,
    ):
        if suffix not in {".pdf", ".md", ".txt"}:
            raise ValueError("parser 输入后缀不受支持")
        directory = self.version_dir(document_id, version_sha256)
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(dir=directory, suffix=suffix, delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(data)
            yield temporary
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_write(self, destination: Path, data: bytes) -> Path:
        self._assert_within_root(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _validate_system_id(self, value: str) -> None:
        if not _SYSTEM_ID.fullmatch(value):
            raise ValueError("文档路径必须使用系统标识")

    def _assert_within_root(self, path: Path) -> None:
        root = self.root.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("artifact 路径越界")
