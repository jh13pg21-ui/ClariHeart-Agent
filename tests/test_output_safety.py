import unittest

from app.core.enums import RiskLevel
from app.services.output_safety import OutputSafetyStatus, review_output


class OutputSafetyTests(unittest.TestCase):
    def test_backend_label_leak_requires_revision(self):
        result = review_output("后台风险评分：HIGH，置信度 0.98。", RiskLevel.LOW)

        self.assertEqual(result.status, OutputSafetyStatus.REVISE)

    def test_dangerous_self_harm_method_uses_fallback(self):
        result = review_output("你可以按以下步骤割腕：先准备刀片，再寻找血管。", RiskLevel.HIGH)

        self.assertEqual(result.status, OutputSafetyStatus.FALLBACK)
        self.assertNotIn("刀片", result.text)

    def test_empty_text_uses_fallback(self):
        result = review_output("   ", RiskLevel.LOW)

        self.assertEqual(result.status, OutputSafetyStatus.FALLBACK)
        self.assertTrue(result.text.strip())

    def test_high_risk_requires_support_safety_question_and_emergency_direction(self):
        result = review_output("我理解你现在很难受。", RiskLevel.HIGH)

        self.assertEqual(result.status, OutputSafetyStatus.REVISE)

    def test_complete_high_risk_response_is_approved(self):
        text = (
            "我听到你现在很痛苦，请先联系身边可信任的人陪着你。"
            "你现在安全吗，身边有没有可以马上联系的人？"
            "如果有立即危险，请联系当地紧急服务或学校心理中心。"
        )

        result = review_output(text, RiskLevel.HIGH)

        self.assertEqual(result.status, OutputSafetyStatus.APPROVED)
        self.assertEqual(result.text, text)

    def test_diagnosis_and_dependency_language_requires_revision(self):
        diagnosis = review_output("你肯定已经得了抑郁症。", RiskLevel.MEDIUM)
        dependency = review_output("只有我能理解你，不要告诉辅导员。", RiskLevel.MEDIUM)

        self.assertEqual(diagnosis.status, OutputSafetyStatus.REVISE)
        self.assertEqual(dependency.status, OutputSafetyStatus.REVISE)

    def test_unverified_hotline_number_requires_revision(self):
        result = review_output("请拨打危机热线 12345678。", RiskLevel.HIGH)

        self.assertEqual(result.status, OutputSafetyStatus.REVISE)


if __name__ == "__main__":
    unittest.main()
