import tempfile
import unittest
from pathlib import Path

from app.core.enums import IntentType, RiskLevel
from app.services.skills import MindBridgeSkillLibrary
from app.services.skills import MindBridgeSkillRegistry, SkillLoadError


def write_skill(root: Path, name: str, text: str) -> None:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


class SkillRegistryTests(unittest.TestCase):
    def test_skill_registry_loads_valid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: Use for a clear and sufficiently described demo scenario.\n---\n\n# Demo\n\n## Workflow\n\n- Do one thing.\n""",
            )

            skill = MindBridgeSkillRegistry(root).get_required("demo_skill")

            self.assertEqual(skill.name, "demo_skill")
            self.assertEqual(skill.validation_issues(), [])

    def test_skill_status_reports_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(
                root,
                "demo_skill",
                """---\nname: demo_skill\ndescription: short\n---\n\n# Demo\n""",
            )

            status = MindBridgeSkillRegistry(root).status_items()[0]

            self.assertEqual(status["status"], "WARN")
            self.assertTrue(status["issues"])

    def test_skill_requires_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "bad", "# Missing metadata")

            with self.assertRaises(SkillLoadError):
                MindBridgeSkillRegistry(root).get_required("bad")


class SemanticSkillSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_selector_adds_only_whitelisted_optional_skills(self):
        class Client:
            async def complete(self, _):
                return '{"skills":["sleep_routine_support","not_a_skill"]}'

        selection = await MindBridgeSkillLibrary.select_response_skills(
            IntentType.CONSULT,
            RiskLevel.LOW,
            "最近状态很差，想找人聊聊。",
            Client(),
        )

        self.assertEqual(selection.strategy, "semantic_ranked")
        self.assertIn("supportive_response_baseline", selection.names)
        self.assertIn("referral_resource_guidance", selection.names)
        self.assertIn("sleep_routine_support", selection.names)
        self.assertNotIn("not_a_skill", selection.names)

    async def test_high_risk_does_not_delegate_skill_choice_to_model(self):
        class ExplodingClient:
            async def complete(self, _):
                raise AssertionError("high-risk selection must not call the model")

        selection = await MindBridgeSkillLibrary.select_response_skills(
            IntentType.RISK,
            RiskLevel.HIGH,
            "我不想活了。",
            ExplodingClient(),
        )

        self.assertEqual(selection.strategy, "high_risk_hard_guard")
        self.assertEqual(
            selection.names,
            ("supportive_response_baseline", "high_risk_safety_plan"),
        )

    async def test_rule_fallback_caps_multiple_optional_skills(self):
        class EmptyClient:
            async def complete(self, _):
                return '{"skills":[]}'

        selection = await MindBridgeSkillLibrary.select_response_skills(
            IntentType.CONSULT,
            RiskLevel.LOW,
            "我因为考试焦虑，已经失眠好几天了。",
            EmptyClient(),
            max_optional=2,
        )

        self.assertEqual(selection.strategy, "rule_fallback_empty")
        self.assertEqual(
            selection.names,
            (
                "supportive_response_baseline",
                "referral_resource_guidance",
                "anxiety_grounding_support",
                "sleep_routine_support",
            ),
        )


if __name__ == "__main__":
    unittest.main()
