import unittest

from app.core.config import Settings
from app.rag_ingestion.errors import RetryableIngestionError, sanitize_error


class RagIngestionConfigTests(unittest.TestCase):
    def test_vision_credentials_fall_back_to_existing_openai_settings(self):
        settings = Settings(
            app_environment="test",
            openai_base_url="https://proxy.example/v1/",
            openai_api_key="existing-secret",
        )

        self.assertEqual(settings.effective_rag_vision_base_url, "https://proxy.example/v1")
        self.assertEqual(settings.effective_rag_vision_api_key, "existing-secret")

    def test_explicit_vision_credentials_override_existing_openai_settings(self):
        settings = Settings(
            app_environment="test",
            openai_base_url="https://embedding.example/v1",
            openai_api_key="embedding-secret",
            rag_vision_base_url="https://vision.example/v1/",
            rag_vision_api_key="vision-secret",
        )

        self.assertEqual(settings.effective_rag_vision_base_url, "https://vision.example/v1")
        self.assertEqual(settings.effective_rag_vision_api_key, "vision-secret")


class RagIngestionErrorTests(unittest.TestCase):
    def test_sanitize_error_removes_credentials_and_keeps_error_contract(self):
        error = RetryableIngestionError(
            "Authorization: Bearer secret-value; api_key=another-secret",
            code="VISION_RATE_LIMITED",
        )

        code, message, retryable = sanitize_error(error)

        self.assertEqual(code, "VISION_RATE_LIMITED")
        self.assertTrue(retryable)
        self.assertNotIn("secret-value", message)
        self.assertNotIn("another-secret", message)
        self.assertNotIn("Bearer", message)


if __name__ == "__main__":
    unittest.main()
