from __future__ import annotations

import re
from collections.abc import Mapping

from app.rag_ingestion.evaluation.dataset import DeepDataset, RoutingDataset
from app.rag_ingestion.schema import StructureStrategy


def evaluate_routing(dataset: RoutingDataset, predictions: Mapping[tuple[str, int], object]) -> dict:
    total = exact = text_correct = structure_correct = 0
    vision_total = vision_hit = simple_total = simple_false_positive = 0
    for document in dataset.documents:
        for page in document.pages:
            total += 1
            prediction = predictions.get((document.filename, page.page_number))
            if prediction is None:
                continue
            text = _value(prediction, "text_strategy")
            structure = _value(prediction, "structure_strategy")
            text_ok = text == page.text_strategy.value
            structure_ok = structure == page.structure_strategy.value
            text_correct += int(text_ok)
            structure_correct += int(structure_ok)
            exact += int(text_ok and structure_ok)
            if page.structure_strategy == StructureStrategy.VISION:
                vision_total += 1
                vision_hit += int(structure == StructureStrategy.VISION.value)
            else:
                simple_total += 1
                simple_false_positive += int(structure == StructureStrategy.VISION.value)
    covered = sum(1 for key in predictions if _known_key(dataset, key))
    return {
        "annotation_status": dataset.annotation_status,
        "pages": total,
        "coverage": _ratio(covered, total),
        "exact_route_accuracy": _ratio(exact, total),
        "text_route_accuracy": _ratio(text_correct, total),
        "structure_route_accuracy": _ratio(structure_correct, total),
        "vision_recall": _ratio(vision_hit, vision_total),
        "simple_vision_false_positive_rate": _ratio(simple_false_positive, simple_total),
    }


def evaluate_deep_pages(dataset: DeepDataset, outputs: Mapping[tuple[str, int], dict]) -> dict:
    total = len(dataset.pages)
    schema = reading = table = citation = 0
    expected_text = recalled_text = 0
    for page in dataset.pages:
        output = outputs.get((page.filename, page.page_number), {})
        schema += int(bool(output.get("schema_valid")))
        reading += int(bool(output.get("reading_order_ok")))
        if any("table" in relation.lower() for relation in page.relations):
            table += int(bool(output.get("table_relation_ok")))
        else:
            table += 1
        normalized = _normalize(str(output.get("text", "")))
        expected_text += len(page.key_text)
        recalled_text += sum(int(_normalize(item) in normalized) for item in page.key_text)
        cited = {int(value) for value in output.get("citation_pages", [])}
        citation += int(bool(cited.intersection(page.evidence_pages)))
    return {
        "annotation_status": dataset.annotation_status,
        "pages": total,
        "coverage": _ratio(len(outputs), total),
        "schema_valid_rate": _ratio(schema, total),
        "key_text_recall": _ratio(recalled_text, expected_text),
        "reading_order_accuracy": _ratio(reading, total),
        "table_relation_accuracy": _ratio(table, total),
        "citation_page_hit_rate": _ratio(citation, total),
    }


def _value(prediction: object, key: str) -> str:
    value = prediction.get(key) if isinstance(prediction, Mapping) else getattr(prediction, key, "")
    return getattr(value, "value", str(value))


def _known_key(dataset: RoutingDataset, key: tuple[str, int]) -> bool:
    return any(
        document.filename == key[0] and 1 <= key[1] <= document.page_count
        for document in dataset.documents
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0
