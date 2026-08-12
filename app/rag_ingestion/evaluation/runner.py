from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.rag_ingestion.evaluation.dataset import load_deep_pages, load_routing_gold
from app.rag_ingestion.evaluation.metrics import evaluate_deep_pages, evaluate_routing


MINIMUMS = {
    "routing": {
        "coverage": 1.0,
        "exact_route_accuracy": 0.90,
        "vision_recall": 0.95,
    },
    "deep": {
        "coverage": 1.0,
        "schema_valid_rate": 0.99,
        "key_text_recall": 0.90,
        "reading_order_accuracy": 0.90,
        "table_relation_accuracy": 0.90,
        "citation_page_hit_rate": 0.95,
    },
}
MAXIMUMS = {"routing": {"simple_vision_false_positive_rate": 0.10}}


def threshold_failures(routing: dict, deep: dict) -> list[str]:
    failures = []
    for group, report in (("routing", routing), ("deep", deep)):
        for metric, minimum in MINIMUMS[group].items():
            actual = float(report.get(metric, 0.0))
            if actual < minimum:
                failures.append(f"{group}.{metric}={actual:.4f} < {minimum:.4f}")
        for metric, maximum in MAXIMUMS.get(group, {}).items():
            actual = float(report.get(metric, 1.0))
            if actual > maximum:
                failures.append(f"{group}.{metric}={actual:.4f} > {maximum:.4f}")
    return failures


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="MindBridge RAG 摄取离线评测")
    parser.add_argument(
        "--routing-gold",
        type=Path,
        default=root / "app/rag_eval/gold/routing-gold-v1.json",
    )
    parser.add_argument(
        "--deep-gold",
        type=Path,
        default=root / "app/rag_eval/gold/deep-pages-v1.json",
    )
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output", type=Path, default=root / "target/rag-ingestion-eval.json")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--allow-seed", action="store_true")
    args = parser.parse_args(argv)

    routing_gold = load_routing_gold(args.routing_gold)
    deep_gold = load_deep_pages(args.deep_gold)
    if args.validate_only:
        report = {
            "routingPages": sum(len(document.pages) for document in routing_gold.documents),
            "deepPages": len(deep_gold.pages),
            "annotationStatus": routing_gold.annotation_status,
        }
        _write_report(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.predictions is None:
        parser.error("正常评测必须提供 --predictions")

    raw = json.loads(args.predictions.read_text(encoding="utf-8"))
    routing_predictions = {
        (item["filename"], int(item["page_number"])): item
        for item in raw.get("routing", [])
    }
    deep_predictions = {
        (item["filename"], int(item["page_number"])): item
        for item in raw.get("deep", [])
    }
    routing_report = evaluate_routing(routing_gold, routing_predictions)
    deep_report = evaluate_deep_pages(deep_gold, deep_predictions)
    failures = threshold_failures(routing_report, deep_report)
    seed = "requires_human_review" in routing_gold.annotation_status
    if seed and not args.allow_seed:
        failures.append("routing annotations are machine seed labels and require human review")
    report = {
        "routing": routing_report,
        "deep": deep_report,
        "releaseGateEligible": not seed,
        "passed": not failures,
        "failures": failures,
    }
    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
