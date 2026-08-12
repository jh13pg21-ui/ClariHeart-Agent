import json
import asyncio
import unittest
from contextlib import redirect_stdout
from io import StringIO


class ContextRecoveryEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_versioned_dataset_covers_required_fault_matrix(self):
        from app.context_eval.runner import load_dataset

        dataset = load_dataset()
        faults = {case.fault for case in dataset.cases}

        self.assertEqual(dataset.version, "context-recovery-v1")
        self.assertTrue(
            {"none", "prompt_too_long", "429", "529", "timeout", "invalid_json", "stream_interrupted", "redis_unavailable"}
            <= faults
        )

    async def test_all_fault_cases_meet_recovery_and_privacy_thresholds(self):
        from app.context_eval.runner import evaluate_dataset

        report = await evaluate_dataset()

        self.assertTrue(report.passed, report.to_dict())
        self.assertEqual(report.failed_cases, 0)
        self.assertGreater(report.aggregate["tokenReductionRatio"], 0.2)
        self.assertTrue(all(case.required_sections_preserved for case in report.cases))
        self.assertTrue(
            all(case.cloud_calls == 0 for case in report.cases if case.risk_level == "HIGH")
        )

    async def test_prompt_overflow_uses_exactly_one_reactive_compaction(self):
        from app.context_eval.runner import evaluate_dataset

        report = await evaluate_dataset(case_ids={"context-overflow"})
        case = report.cases[0]

        self.assertTrue(case.passed)
        self.assertTrue(case.reactive)
        self.assertEqual(case.reactive_compactions, 1)
        self.assertLess(case.tokens_after, case.tokens_before)
        self.assertEqual(case.cloud_calls, 0)

    async def test_cli_json_is_machine_readable_and_contains_no_context_content(self):
        from app.context_eval.runner import main

        output = StringIO()
        with redirect_stdout(output):
            exit_code = await asyncio.to_thread(main, ["--json"])
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["passed"])
        self.assertNotIn("EVAL_SECRET_SENTINEL", output.getvalue())


if __name__ == "__main__":
    unittest.main()
