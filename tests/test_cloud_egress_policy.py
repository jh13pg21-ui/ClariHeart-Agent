from dataclasses import replace

import pytest

from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest


def _request(**changes: object) -> ModelRequest:
    request = ModelRequest(
        request_id="req-egress",
        agent_name="ResponseAgent",
        task_name="respond",
        risk_level=RiskLevel.LOW,
        messages=(),
        preferred_provider="ollama",
        preferred_model="local-model",
        max_output_tokens=512,
    )
    return replace(request, **changes)


def test_high_risk_never_allows_cloud_even_when_caller_sets_all_flags():
    from app.llm.egress import CloudEgressPolicy

    decision = CloudEgressPolicy().evaluate(
        _request(
            risk_level=RiskLevel.HIGH,
            cloud_egress_allowed=True,
            sanitized=True,
            context_section_ids=("current-input",),
        )
    )

    assert decision.allowed is False
    assert decision.reason == "high_risk_must_remain_local"
    assert decision.permitted_section_ids == ()


@pytest.mark.parametrize("risk_level", [RiskLevel.LOW, RiskLevel.MEDIUM])
def test_low_and_medium_require_explicit_cloud_permission(risk_level: RiskLevel):
    from app.llm.egress import CloudEgressPolicy

    decision = CloudEgressPolicy().evaluate(
        _request(risk_level=risk_level, sanitized=True, cloud_egress_allowed=False)
    )

    assert decision.allowed is False
    assert decision.reason == "cloud_egress_not_authorized"


@pytest.mark.parametrize("risk_level", [RiskLevel.LOW, RiskLevel.MEDIUM])
def test_low_and_medium_require_sanitization_proof(risk_level: RiskLevel):
    from app.llm.egress import CloudEgressPolicy

    decision = CloudEgressPolicy().evaluate(
        _request(risk_level=risk_level, sanitized=False, cloud_egress_allowed=True)
    )

    assert decision.allowed is False
    assert decision.reason == "request_not_sanitized"


def test_sanitized_low_risk_request_exposes_only_selected_section_ids():
    from app.llm.egress import CloudEgressPolicy

    decision = CloudEgressPolicy().evaluate(
        _request(
            cloud_egress_allowed=True,
            sanitized=True,
            context_section_ids=("system-core", "current-input"),
        )
    )

    assert decision.allowed is True
    assert decision.reason == "cloud_egress_allowed"
    assert decision.permitted_section_ids == ("system-core", "current-input")


def test_policy_rejects_duplicate_or_blank_section_ids_fail_closed():
    from app.llm.egress import CloudEgressPolicy

    duplicate = CloudEgressPolicy().evaluate(
        _request(
            cloud_egress_allowed=True,
            sanitized=True,
            context_section_ids=("current-input", "current-input"),
        )
    )
    blank = CloudEgressPolicy().evaluate(
        _request(
            cloud_egress_allowed=True,
            sanitized=True,
            context_section_ids=("current-input", ""),
        )
    )

    assert duplicate.reason == "invalid_context_section_manifest"
    assert blank.reason == "invalid_context_section_manifest"
    assert duplicate.allowed is blank.allowed is False
