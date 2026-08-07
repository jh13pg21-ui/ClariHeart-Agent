from app.core.config import Settings


def test_production_defaults_enable_safe_runtime_layers():
    settings = Settings(_env_file=None)

    assert settings.model_gateway_enabled is True
    assert settings.prompt_registry_enabled is True
    assert settings.context_planner_enabled is True
    assert settings.recovery_orchestrator_enabled is True
    assert settings.memory_v2_enabled is True
    assert settings.context_planner_shadow_mode is False
    assert settings.memory_v2_shadow_mode is False
    assert settings.memory_consolidation_enabled is True


def test_high_risk_cloud_egress_cannot_be_enabled_by_environment(monkeypatch):
    monkeypatch.setenv("HIGH_RISK_CLOUD_EGRESS_ALLOWED", "true")

    settings = Settings(_env_file=None)

    assert not hasattr(settings, "high_risk_cloud_egress_allowed")
