from types import SimpleNamespace

from app.core.enums import RiskLevel
from app.llm.capabilities import ModelCapabilitiesRegistry
from app.llm.contracts import ModelRequest


def _settings(**overrides):
    values = {
        "ollama_model": "qwen-local",
        "openai_model": "cloud-model",
        "model_context_window_default": 8192,
        "model_ollama_context_window": 32768,
        "model_ollama_max_output_tokens": 4096,
        "model_openai_context_window": 128000,
        "model_openai_max_output_tokens": 16384,
        "ai_max_tokens": 512,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_registry_uses_explicit_provider_capabilities():
    registry = ModelCapabilitiesRegistry(_settings())

    local = registry.for_model("ollama", "qwen-local")
    cloud = registry.for_model("openai", "cloud-model")

    assert local.context_window == 32768
    assert local.maximum_output_tokens == 4096
    assert local.cloud is False
    assert cloud.context_window == 128000
    assert cloud.maximum_output_tokens == 16384
    assert cloud.cloud is True


def test_unknown_provider_uses_conservative_defaults_without_becoming_cloud():
    capability = ModelCapabilitiesRegistry(_settings()).for_model("private", "unknown")

    assert capability.context_window == 8192
    assert capability.maximum_output_tokens == 512
    assert capability.cloud is False


def test_high_risk_model_request_defaults_to_no_cloud_egress():
    request = ModelRequest(
        request_id="request-1",
        agent_name="SafetyAgent",
        task_name="risk_assessment",
        risk_level=RiskLevel.HIGH,
        messages=(),
        preferred_provider="ollama",
        preferred_model="qwen-local",
        max_output_tokens=512,
    )

    assert request.cloud_egress_allowed is False
    assert request.sanitized is False
    assert request.stream is False

