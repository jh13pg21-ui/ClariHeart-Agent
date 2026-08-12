import unittest

from app.services.model_trace import ModelTraceEvent
from app.services.runtime_metrics import RuntimeMetrics


class RuntimeMetricsTests(unittest.TestCase):
    def test_declared_labels_exclude_high_cardinality_identifiers(self):
        labels = RuntimeMetrics.declared_label_names()

        self.assertNotIn("user_id", labels)
        self.assertNotIn("session_id", labels)
        self.assertNotIn("request_id", labels)
        self.assertTrue({"provider", "model", "status", "risk", "release"} <= labels)

    def test_snapshot_contains_aggregates_without_raw_request_data(self):
        metrics = RuntimeMetrics()
        metrics.record(
            ModelTraceEvent(
                kind="success",
                request_id="secret-request-id",
                provider="ollama",
                model="qwen",
                risk_level="LOW",
                prompt_release="2026.08-v1",
                input_tokens=100,
                output_tokens=20,
                latency_ms=50,
            )
        )
        metrics.record_context_plan(
            provider="ollama",
            model="qwen",
            tokens_before=500,
            tokens_after=300,
            actions=[("L2", "history_summary")],
        )

        snapshot = metrics.snapshot()
        serialized = str(snapshot)
        self.assertEqual(snapshot["modelCalls"]["total"], 1)
        self.assertEqual(snapshot["context"]["tokensBefore"], 500)
        self.assertEqual(snapshot["context"]["tokensAfter"], 300)
        self.assertNotIn("secret-request-id", serialized)
