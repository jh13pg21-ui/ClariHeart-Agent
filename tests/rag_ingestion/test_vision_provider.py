from __future__ import annotations

import json

import httpx
import pytest

from app.rag_ingestion.errors import CloudVisionForbidden, VisionSchemaInvalid
from app.rag_ingestion.schema import AccessClass, VisionAnalysisRequest
from app.rag_ingestion.vision.openai_compatible import OpenAICompatibleVisionProvider


def request(tmp_path, access=AccessClass.BUILTIN_PUBLIC, allowed=True):
    image = tmp_path / "page.png"
    image.write_bytes(b"png-data")
    return VisionAnalysisRequest(
        page_number=1,
        image_path=image,
        access_class=access,
        cloud_vision_allowed=allowed,
        native_text="政策编号 12345",
    )


def chat_response(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})


def test_vision_payload_uses_original_detail_and_locally_validates_schema(tmp_path):
    payloads = []

    def handler(req: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(req.content))
        return chat_response({"page_number": 1, "page_type": "policy", "blocks": [], "warnings": []})

    provider = OpenAICompatibleVisionProvider(
        base_url="https://proxy.example/v1/",
        api_key="secret",
        model="gpt-5.6-luna",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.analyze(request(tmp_path))

    assert result.page_type == "policy"
    assert payloads[0]["model"] == "gpt-5.6-luna"
    image_part = payloads[0]["messages"][0]["content"][1]
    assert image_part["image_url"]["detail"] == "original"
    assert payloads[0]["response_format"]["type"] == "json_schema"
    prompt = payloads[0]["messages"][0]["content"][0]["text"]
    assert "Allowed top-level keys exactly: page_number, page_type, blocks, warnings" in prompt
    assert "Allowed block type values exactly:" in prompt
    assert "document_identifier" not in prompt
    assert '"blocks": []' in prompt
    assert_strict_object_schema(payloads[0]["response_format"]["json_schema"]["schema"])


def assert_strict_object_schema(node):
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert node.get("required") == list(properties)
            assert node.get("additionalProperties") is False
        assert "default" not in node
        for value in node.values():
            assert_strict_object_schema(value)
    elif isinstance(node, list):
        for value in node:
            assert_strict_object_schema(value)


def test_translated_schema_keys_retry_once_then_fail(tmp_path):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response({"页码": 1, "页面类型": "政策", "区块": []})

    provider = OpenAICompatibleVisionProvider(
        base_url="https://proxy.example/v1",
        api_key="secret",
        model="gpt-5.6-luna",
        max_attempts=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(VisionSchemaInvalid):
        provider.analyze(request(tmp_path))
    assert calls == 2


def test_private_page_is_rejected_before_http_request(tmp_path):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response({"page_number": 1, "blocks": []})

    provider = OpenAICompatibleVisionProvider(
        base_url="https://proxy.example/v1",
        api_key="secret",
        model="gpt-5.6-luna",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CloudVisionForbidden):
        provider.analyze(request(tmp_path, AccessClass.ADMIN_PRIVATE, False))
    assert calls == 0
