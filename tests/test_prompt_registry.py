import tempfile
import unittest
from pathlib import Path


class PromptRegistryTests(unittest.TestCase):
    def test_default_registry_has_explicit_global_and_agent_contracts(self):
        from app.prompts.registry import default_prompt_registry

        registry = default_prompt_registry()

        self.assertEqual(
            set(registry.prompt_ids),
            {
                "global.identity",
                "global.safety_boundary",
                "global.privacy_boundary",
                "global.untrusted_context",
                "agent.understanding",
                "agent.safety",
                "agent.context",
                "agent.response",
                "agent.coordinator",
            },
        )

    def test_missing_template_variable_fails_closed(self):
        from app.prompts.registry import PromptRenderError, default_prompt_registry

        registry = default_prompt_registry()

        with self.assertRaises(PromptRenderError):
            registry.render("agent.response", {"mode": "support"})

    def test_unexpected_template_variable_fails_closed(self):
        from app.prompts.registry import PromptRenderError, default_prompt_registry

        registry = default_prompt_registry()

        with self.assertRaises(PromptRenderError):
            registry.render(
                "agent.response",
                {"mode": "support", "locale": "zh-CN", "user_input": "不可信"},
            )

    def test_render_uses_strict_variables_and_returns_versioned_content(self):
        from app.prompts.registry import default_prompt_registry

        rendered = default_prompt_registry().render(
            "agent.response",
            {"mode": "support", "locale": "zh-CN"},
        )

        self.assertEqual(rendered.prompt_id, "agent.response")
        self.assertRegex(rendered.version, r"^\d+\.\d+\.\d+$")
        self.assertIn("support", rendered.content)
        self.assertEqual(len(rendered.content_hash), 64)

    def test_prompt_content_change_requires_version_change(self):
        from app.prompts.registry import (
            PromptDefinition,
            PromptRegistry,
            PromptVersionMismatch,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "prompt.md"
            path.write_text("first", encoding="utf-8")
            definitions = (
                PromptDefinition("fixture", "1.0.0", "prompt.md"),
            )
            registry = PromptRegistry(root, definitions)
            recorded = registry.release_manifest()
            path.write_text("changed", encoding="utf-8")

            with self.assertRaises(PromptVersionMismatch):
                registry.verify_release(recorded)

    def test_definition_cannot_escape_prompt_root(self):
        from app.prompts.registry import PromptDefinition, PromptRegistry, PromptRegistryError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(PromptRegistryError):
                PromptRegistry(
                    root,
                    (PromptDefinition("escape", "1.0.0", "../outside.md"),),
                )
