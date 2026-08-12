from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.rag_ingestion.errors import (
    CloudVisionForbidden,
    IngestionError,
    RetryableIngestionError,
    VisionSchemaInvalid,
)
from app.rag_ingestion.schema import AccessClass, VisionAnalysisRequest, VisionPageResult


class OpenAICompatibleVisionProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        detail: str = "original",
        timeout_seconds: float = 90.0,
        max_attempts: int = 2,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.detail = detail
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self.sleep = sleep

    def analyze(self, request: VisionAnalysisRequest) -> VisionPageResult:
        self._authorize(request)
        if not self.api_key:
            raise IngestionError("Vision Provider 缺少 API Key", code="VISION_NOT_CONFIGURED")
        payload = self._payload(request)
        last_schema_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1 and last_schema_error is not None:
                payload["messages"][0]["content"][0]["text"] += (
                    "\nProtocol correction: output field names are identifiers and MUST NOT be translated."
                )
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_attempts:
                    raise RetryableIngestionError("Vision 网络请求失败", code="VISION_NETWORK_FAILED") from exc
                self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.max_attempts:
                    raise RetryableIngestionError(
                        f"Vision 上游暂时不可用，HTTP {response.status_code}",
                        code="VISION_UPSTREAM_RETRYABLE",
                    )
                self.sleep(min(2 ** (attempt - 1), 4))
                continue
            if response.status_code in {401, 403}:
                raise IngestionError("Vision 上游拒绝凭据", code="VISION_AUTH_FAILED")
            try:
                response.raise_for_status()
                content = self._content(response.json())
                return VisionPageResult.model_validate_json(self._strip_fence(content))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                last_schema_error = exc
                if attempt == self.max_attempts:
                    raise VisionSchemaInvalid("Vision 响应未通过本地 Schema 校验") from exc
        raise VisionSchemaInvalid("Vision 响应未通过本地 Schema 校验")

    @staticmethod
    def _authorize(request: VisionAnalysisRequest) -> None:
        if not request.cloud_vision_allowed or request.access_class == AccessClass.RESTRICTED:
            raise CloudVisionForbidden("该文档页面不允许发送到云端 Vision")

    def _payload(self, request: VisionAnalysisRequest) -> dict:
        image = Path(request.image_path).read_bytes()
        encoded = base64.b64encode(image).decode("ascii")
        prompt = (
            "Analyze this document page. Return JSON only and follow the supplied schema exactly. "
            "Field names are immutable protocol identifiers. Use visible evidence only; never invent numbers, "
            "policy clauses, hotline numbers, disease names, or procedures.\n"
            "Allowed top-level keys exactly: page_number, page_type, blocks, warnings. "
            "The blocks field is always required. Each block may use only type, reading_order, bbox_norm, "
            "text, section_path, confidence, table, figure. "
            "Allowed block type values exactly: title, heading, paragraph, list, list_item, code, table, "
            "table_cell, figure, caption, comic_panel, equation, header, footer, page_number. "
            "If no reliable blocks exist, return this exact shape: "
            "{\"page_number\": 1, \"page_type\": \"unknown\", \"blocks\": [], \"warnings\": []}.\n"
            f"page_number={request.page_number}\n"
            f"native_text={request.native_text[:12000]}\n"
            f"ocr_text={request.ocr_text[:12000]}"
        )
        return {
            "model": self.model,
            "store": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": self.detail},
                        },
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vision_page_result",
                    "strict": True,
                    "schema": self._strict_schema(VisionPageResult.model_json_schema()),
                },
            },
        }

    @classmethod
    def _strict_schema(cls, node):
        if isinstance(node, list):
            return [cls._strict_schema(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {
            key: cls._strict_schema(value)
            for key, value in node.items()
            if key != "default"
        }
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result

    @staticmethod
    def _content(body: dict) -> str:
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        raise TypeError("Vision content 类型非法")

    @staticmethod
    def _strip_fence(content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            first_newline = stripped.find("\n")
            return stripped[first_newline + 1 : -3].strip()
        return stripped
