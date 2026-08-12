from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag_ingestion.schema import StructureStrategy, TextStrategy


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutingLabel(EvalModel):
    text_strategy: TextStrategy
    structure_strategy: StructureStrategy
    page_type: str
    reasons: list[str] = Field(default_factory=list)
    cloud_vision_allowed: bool = True
    table: bool = False
    multi_column: bool = False
    image_text: bool = False
    comic: bool = False
    infographic: bool = False
    searchable: bool = True


class RoutingRange(RoutingLabel):
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def check_range(self):
        if self.end < self.start:
            raise ValueError("路由范围的 end 不能小于 start")
        return self


class RoutingPage(RoutingLabel):
    page_number: int = Field(ge=1)


class RoutingDocument(EvalModel):
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(gt=0)
    pages: list[RoutingPage]

    @model_validator(mode="after")
    def check_pages(self):
        numbers = [page.page_number for page in self.pages]
        if numbers != list(range(1, self.page_count + 1)):
            raise ValueError(f"{self.filename} 的路由标注没有逐页且仅覆盖一次")
        return self


class RoutingDataset(EvalModel):
    version: str
    annotation_status: str
    documents: list[RoutingDocument]


class DeepPage(EvalModel):
    filename: str
    page_number: int = Field(ge=1)
    page_type: str
    key_text: list[str] = Field(min_length=1)
    expected_section_path: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    question: str
    evidence_pages: list[int] = Field(min_length=1)


class DeepDataset(EvalModel):
    version: str
    annotation_status: str
    pages: list[DeepPage]


def load_routing_gold(path: str | Path) -> RoutingDataset:
    raw = _read_json(path)
    documents = []
    for document in raw.get("documents", []):
        expanded: dict[int, dict] = {}
        for page_range in document.get("page_ranges", []):
            label = {key: value for key, value in page_range.items() if key not in {"start", "end"}}
            for page_number in range(page_range["start"], page_range["end"] + 1):
                if page_number in expanded:
                    raise ValueError(f"重复的页级路由标注: {document['filename']}#{page_number}")
                expanded[page_number] = {"page_number": page_number, **label}
        documents.append(
            {
                "filename": document["filename"],
                "sha256": document["sha256"],
                "page_count": document["page_count"],
                "pages": [expanded[number] for number in sorted(expanded)],
            }
        )
    return RoutingDataset.model_validate(
        {
            "version": raw.get("version"),
            "annotation_status": raw.get("annotation_status"),
            "documents": documents,
        }
    )


def load_deep_pages(path: str | Path) -> DeepDataset:
    return DeepDataset.model_validate(_read_json(path))


def _read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
