import json
import unittest
from pathlib import Path

from app.risk_eval.runner import evaluate_cases
from app.services.risk_rules import detect_risk_signal


class RiskRuleTests(unittest.TestCase):
    def test_negation_and_research_context_do_not_trigger_high_guard(self):
        self.assertIsNone(detect_risk_signal("我没有想过自杀，只是压力很大").level)
        self.assertIsNone(detect_risk_signal("论文研究大学生自杀预防").level)

    def test_offline_dataset_meets_safety_thresholds(self):
        path = Path(__file__).resolve().parents[1] / "app" / "risk_eval" / "mindbridge-risk-eval.json"
        report = evaluate_cases(json.loads(path.read_text(encoding="utf-8")))

        self.assertGreaterEqual(report["totalCases"], 30)
        self.assertGreaterEqual(report["highRiskRecall"], 0.95)
        self.assertGreaterEqual(report["macroF1"], 0.80)
        self.assertEqual(report["highRiskFalseNegatives"], 0)


if __name__ == "__main__":
    unittest.main()
