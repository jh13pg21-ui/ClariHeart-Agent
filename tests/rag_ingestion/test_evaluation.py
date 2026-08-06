from __future__ import annotations

import hashlib
from pathlib import Path

from app.rag_ingestion.evaluation.dataset import load_deep_pages, load_routing_gold
from app.rag_ingestion.evaluation.metrics import evaluate_deep_pages, evaluate_routing
from app.rag_ingestion.evaluation.runner import threshold_failures


ROOT = Path(__file__).resolve().parents[2]


def test_routing_gold_covers_every_pdf_page_once_and_hashes_match():
    dataset = load_routing_gold(ROOT / "app/rag_eval/gold/routing-gold-v1.json")
    keys = [
        (document.filename, page.page_number)
        for document in dataset.documents
        for page in document.pages
    ]
    assert len(keys) == 270
    assert len(set(keys)) == 270
    for document in dataset.documents:
        path = ROOT / "app/knowledge/pdf" / document.filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == document.sha256


def test_deep_dataset_has_thirty_unique_representative_pages():
    dataset = load_deep_pages(ROOT / "app/rag_eval/gold/deep-pages-v1.json")
    keys = {(page.filename, page.page_number) for page in dataset.pages}
    assert len(dataset.pages) == 30
    assert len(keys) == 30
    assert {page.page_type for page in dataset.pages} >= {
        "policy_text",
        "double_column",
        "handbook",
        "comic",
        "infographic",
    }


def test_metrics_measure_route_errors_and_deep_quality():
    routing = load_routing_gold(ROOT / "app/rag_eval/gold/routing-gold-v1.json")
    predictions = {}
    for document in routing.documents:
        for page in document.pages:
            predictions[(document.filename, page.page_number)] = {
                "text_strategy": page.text_strategy,
                "structure_strategy": page.structure_strategy,
            }
    predictions[(routing.documents[0].filename, 1)] = {
        "text_strategy": "NATIVE",
        "structure_strategy": "LOCAL",
    }
    report = evaluate_routing(routing, predictions)
    assert 0 < report["exact_route_accuracy"] < 1
    assert report["coverage"] == 1

    deep = load_deep_pages(ROOT / "app/rag_eval/gold/deep-pages-v1.json")
    outputs = {
        (page.filename, page.page_number): {
            "schema_valid": True,
            "text": " ".join(page.key_text),
            "reading_order_ok": True,
            "table_relation_ok": True,
            "citation_pages": [page.page_number],
        }
        for page in deep.pages
    }
    deep_report = evaluate_deep_pages(deep, outputs)
    assert deep_report["schema_valid_rate"] == 1
    assert deep_report["key_text_recall"] == 1
    assert deep_report["citation_page_hit_rate"] == 1


def test_release_thresholds_fail_closed():
    failures = threshold_failures(
        {"coverage": 1, "exact_route_accuracy": 0.8, "vision_recall": 1,
         "simple_vision_false_positive_rate": 0.0},
        {"coverage": 1, "schema_valid_rate": 1, "key_text_recall": 1,
         "reading_order_accuracy": 1, "table_relation_accuracy": 1,
         "citation_page_hit_rate": 1},
    )
    assert any("exact_route_accuracy" in failure for failure in failures)
