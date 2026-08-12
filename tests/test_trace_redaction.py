import unittest

from app.services.trace_redaction import redact_collaboration_payload


class TraceRedactionTests(unittest.TestCase):
    def test_memory_body_is_redacted_but_source_metadata_is_kept(self):
        redacted = redact_collaboration_payload(
            {
                "longTermMemories": [
                    {
                        "id": "m1",
                        "body": "敏感正文",
                        "status": "ACTIVE",
                    }
                ]
            }
        )

        self.assertEqual(
            redacted,
            {
                "longTermMemories": [
                    {
                        "id": "m1",
                        "body": "[REDACTED]",
                        "status": "ACTIVE",
                    }
                ]
            },
        )

    def test_nested_prompt_like_fields_are_redacted_recursively(self):
        redacted = redact_collaboration_payload(
            {
                "payload": {
                    "modelHistory": [{"role": "user", "content": "原文"}],
                    "skillContext": "技能正文",
                    "longTermMemoryContext": "长期记忆正文",
                },
                "metadata": {"promptManifestHash": "abc"},
            }
        )

        self.assertEqual(redacted["payload"]["modelHistory"], "[REDACTED]")
        self.assertEqual(redacted["payload"]["skillContext"], "[REDACTED]")
        self.assertEqual(redacted["payload"]["longTermMemoryContext"], "[REDACTED]")
        self.assertEqual(redacted["metadata"]["promptManifestHash"], "abc")
