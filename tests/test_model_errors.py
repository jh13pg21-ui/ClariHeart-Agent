import httpx
import pytest

from app.llm.errors import (
    ModelErrorCode,
    classify_http_error,
    classify_transport_error,
)


@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        (429, ModelErrorCode.RATE_LIMITED, True),
        (529, ModelErrorCode.OVERLOADED, True),
        (503, ModelErrorCode.OVERLOADED, True),
        (401, ModelErrorCode.AUTHENTICATION, False),
        (400, ModelErrorCode.PERMANENT, False),
    ],
)
def test_http_status_is_classified(status, expected_code, retryable):
    response = httpx.Response(
        status,
        headers={"Retry-After": "3"},
        request=httpx.Request("POST", "http://model.test/chat"),
    )

    error = classify_http_error(response, "ollama", "local-model")

    assert error.code == expected_code
    assert error.retryable is retryable
    assert error.retry_after_seconds == 3.0
    assert error.provider == "ollama"
    assert error.model == "local-model"


def test_prompt_too_long_payload_is_not_treated_as_generic_bad_request():
    response = httpx.Response(
        400,
        json={
            "error": {
                "code": "context_length_exceeded",
                "message": "maximum context window reached",
            }
        },
        request=httpx.Request("POST", "http://model.test/chat"),
    )

    error = classify_http_error(response, "openai", "cloud-model")

    assert error.code == ModelErrorCode.PROMPT_TOO_LONG
    assert error.retryable is True


def test_transport_timeout_and_network_errors_remain_distinguishable():
    request = httpx.Request("POST", "http://model.test/chat")

    timeout = classify_transport_error(
        httpx.ReadTimeout("slow", request=request),
        "ollama",
        "local-model",
    )
    network = classify_transport_error(
        httpx.ConnectError("offline", request=request),
        "ollama",
        "local-model",
    )

    assert timeout.code == ModelErrorCode.TIMEOUT
    assert network.code == ModelErrorCode.NETWORK
    assert timeout.retryable is True
    assert network.retryable is True


def test_provider_error_message_is_bounded():
    response = httpx.Response(
        500,
        text="sensitive" * 200,
        request=httpx.Request("POST", "http://model.test/chat"),
    )

    error = classify_http_error(response, "openai", "cloud-model")

    assert len(error.message) <= 500

