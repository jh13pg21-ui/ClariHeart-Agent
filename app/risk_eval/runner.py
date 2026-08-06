from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.services.risk_rules import detect_risk_signal


LABELS = ("LOW", "MEDIUM", "HIGH")


def evaluate_cases(cases: list[dict]) -> dict:
    results = []
    confusion = Counter()
    for case in cases:
        signal = detect_risk_signal(str(case["text"]))
        predicted = signal.level.value if signal.level is not None else "LOW"
        expected = str(case["expected"]).upper()
        confusion[(expected, predicted)] += 1
        results.append(
            {
                **case,
                "predicted": predicted,
                "passed": predicted == expected,
                "reason": signal.reason,
                "matched": signal.matched,
            }
        )
    per_label = {}
    for label in LABELS:
        tp = confusion[(label, label)]
        fp = sum(confusion[(other, label)] for other in LABELS if other != label)
        fn = sum(confusion[(label, other)] for other in LABELS if other != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        per_label[label] = {
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "support": sum(confusion[(label, other)] for other in LABELS),
        }
    return {
        "totalCases": len(cases),
        "accuracy": sum(item["passed"] for item in results) / max(1, len(results)),
        "macroF1": sum(per_label[label]["f1"] for label in LABELS) / len(LABELS),
        "highRiskRecall": per_label["HIGH"]["recall"],
        "highRiskFalseNegatives": sum(
            confusion[("HIGH", label)] for label in ("LOW", "MEDIUM")
        ),
        "perLabel": per_label,
        "confusion": {
            expected: {predicted: confusion[(expected, predicted)] for predicted in LABELS}
            for expected in LABELS
        },
        "results": results,
    }


def run(dataset_path: Path, output_path: Path) -> dict:
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))
    report = {
        "createdAt": datetime.utcnow().isoformat(),
        "evaluator": "deterministic-pre-model-safety-gate",
        **evaluate_cases(cases),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="评测风险识别硬规则安全门")
    parser.add_argument("--dataset", default=settings.risk_eval_dataset)
    parser.add_argument("--output", default=settings.risk_eval_output)
    args = parser.parse_args()
    root = settings.project_root
    report = run(root / args.dataset, root / args.output)
    metrics = {key: report[key] for key in ("totalCases", "accuracy", "macroF1", "highRiskRecall", "highRiskFalseNegatives")}
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if report["highRiskRecall"] >= 0.95 and report["macroF1"] >= 0.80 else 1


if __name__ == "__main__":
    raise SystemExit(main())
