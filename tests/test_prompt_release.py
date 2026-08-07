import unittest


EXPECTED_TASKS = {
    "intent_classification",
    "risk_assessment",
    "response_generation",
    "query_rewrite",
    "conversation_summary",
    "memory_extraction",
    "memory_selection",
    "memory_consolidation",
    "context_section_summary",
}


class PromptReleaseTests(unittest.TestCase):
    def test_release_contains_every_runtime_task(self):
        from app.prompts.release import PROMPT_RELEASE, default_prompt_release

        release = default_prompt_release()

        self.assertEqual(PROMPT_RELEASE, "2026.08-v1")
        self.assertEqual(set(release.tasks), EXPECTED_TASKS)
        self.assertEqual(len(release.manifest_hash), 64)

    def test_schema_parser_version_matches_prompt_definition(self):
        from app.prompts.registry import default_prompt_registry

        registry = default_prompt_registry()
        definition = registry.get("task.conversation_summary")

        self.assertEqual(definition.output_schema_id, "conversation_summary_v2")
        schema = registry.output_schema(definition.output_schema_id)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], 2)

    def test_every_task_has_version_hash_and_registered_schema(self):
        from app.prompts.release import default_prompt_release
        from app.prompts.registry import default_prompt_registry

        registry = default_prompt_registry()
        release = default_prompt_release(registry)

        for task_name in EXPECTED_TASKS:
            entry = release.prompts[f"task.{task_name}"]
            self.assertRegex(entry.version, r"^\d+\.\d+\.\d+$")
            self.assertEqual(len(entry.content_hash), 64)
            definition = registry.get(f"task.{task_name}")
            if definition.output_schema_id:
                self.assertIn(definition.output_schema_id, release.schema_hashes)

    def test_summary_and_memory_schemas_require_evidence_and_cap_arrays(self):
        from app.prompts.registry import default_prompt_registry

        registry = default_prompt_registry()
        summary = registry.output_schema("conversation_summary_v2")
        item = summary["$defs"]["summaryItem"]
        candidate = registry.output_schema("memory_candidate_v2")

        self.assertIn("evidenceMessageIds", item["required"])
        self.assertLessEqual(summary["properties"]["studentConcerns"]["maxItems"], 12)
        self.assertIn("evidenceMessageIds", candidate["items"]["required"])
