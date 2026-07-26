import unittest

from app.services.report import ReportService


class ReportServiceShapeTests(unittest.TestCase):
    def test_expected_methods_are_bound_to_service(self):
        for name in ["latest_reports", "agent_run_traces", "tool_audits", "conversation"]:
            self.assertTrue(callable(getattr(ReportService, name, None)), name)


if __name__ == "__main__":
    unittest.main()
