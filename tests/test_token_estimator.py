import unittest
from types import SimpleNamespace

from app.llm.capabilities import ModelCapabilities
from app.schemas.dtos import AiMessage


def _capabilities(**changes):
    values = {
        "provider": "ollama",
        "model": "qwen",
        "context_window": 32768,
        "default_output_tokens": 512,
        "maximum_output_tokens": 4096,
        "cloud": False,
        "tokenizer_kind": "conservative",
        "tokenizer_path": "",
    }
    values.update(changes)
    return ModelCapabilities(**values)


class TokenEstimatorTests(unittest.TestCase):
    def test_input_budget_reserves_output_recovery_and_margin(self):
        from app.context.tokens import calculate_input_budget

        budget = calculate_input_budget(
            _capabilities(),
            requested_output_tokens=1024,
            reserve=1024,
            margin_ratio=0.10,
        )

        self.assertEqual(budget, 27443)

    def test_invalid_budget_inputs_fail_closed(self):
        from app.context.tokens import TokenBudgetError, calculate_input_budget

        with self.assertRaises(TokenBudgetError):
            calculate_input_budget(
                _capabilities(context_window=1024),
                requested_output_tokens=900,
                reserve=200,
                margin_ratio=0.1,
            )
        with self.assertRaises(TokenBudgetError):
            calculate_input_budget(
                _capabilities(),
                requested_output_tokens=100,
                reserve=0,
                margin_ratio=1.0,
            )

    def test_conservative_estimator_never_returns_zero_for_nonempty_unicode(self):
        from app.context.tokens import ConservativeEstimator

        estimator = ConservativeEstimator()

        self.assertGreaterEqual(estimator.count("秋招压力很大"), 6)
        self.assertEqual(estimator.count(""), 0)

    def test_message_overhead_is_included(self):
        from app.context.tokens import ConservativeEstimator

        estimator = ConservativeEstimator()
        messages = [AiMessage(role="user", content="hi")]

        self.assertGreater(estimator.count_messages(messages), estimator.count("hi"))

    def test_registry_applies_configured_safety_multiplier(self):
        from app.context.tokens import TokenEstimatorRegistry

        settings = SimpleNamespace(model_token_estimator_safety_multiplier=1.5)
        registry = TokenEstimatorRegistry(settings)
        estimator = registry.for_model(_capabilities())

        self.assertEqual(estimator.count("秋招"), 3)

    def test_registry_uses_tiktoken_for_openai_capability(self):
        from app.context.tokens import TiktokenEstimator, TokenEstimatorRegistry

        estimator = TokenEstimatorRegistry(SimpleNamespace()).for_model(
            _capabilities(
                provider="openai",
                model="gpt-4o-mini",
                cloud=True,
                tokenizer_kind="tiktoken",
            )
        )

        self.assertIsInstance(estimator.inner, TiktokenEstimator)
        self.assertGreater(estimator.count("hello world"), 0)
