from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from app.llm.contracts import ModelRequest, ModelResult, ModelStreamEvent
from app.llm.errors import (
    ModelError,
    ModelErrorCode,
    classify_http_error,
    classify_transport_error,
)


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResult:
        ...

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        ...


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        base_url: str,
        *,
        temperature: float = 0.35,
    ):
        self.http_client = http_client
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature

    async def complete(self, request: ModelRequest) -> ModelResult:
        started = time.perf_counter()
        try:
            response = await self.http_client.post(
                f"{self.base_url}/api/chat",
                json=self._payload(request, stream=False),
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise classify_transport_error(
                exc,
                self.name,
                request.preferred_model,
            ) from exc
        if response.is_error:
            raise classify_http_error(response, self.name, request.preferred_model)
        payload = _response_json(response, self.name, request.preferred_model)
        message = payload.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise _invalid_response(self.name, request.preferred_model, "missing message.content")
        text = str(message["content"])
        finish_reason = str(payload.get("done_reason") or "stop")
        return ModelResult(
            text=text,
            provider=self.name,
            model=request.preferred_model,
            finish_reason=finish_reason,
            input_tokens=_nonnegative_int(payload.get("prompt_eval_count")),
            output_tokens=_nonnegative_int(payload.get("eval_count")),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            request_id=request.request_id,
            provider_request_id=response.headers.get("x-request-id", ""),
            partial=_is_truncated(finish_reason),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        completed = False
        try:
            async with self.http_client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=self._payload(request, stream=True),
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise classify_http_error(response, self.name, request.preferred_model)
                provider_request_id = response.headers.get("x-request-id", "")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise _invalid_response(self.name, request.preferred_model, "invalid stream JSON") from exc
                    message = payload.get("message") or {}
                    token = message.get("content", "") if isinstance(message, dict) else ""
                    if token:
                        yield ModelStreamEvent(
                            kind="token",
                            request_id=request.request_id,
                            text=str(token),
                            provider_request_id=provider_request_id,
                        )
                    if payload.get("done"):
                        completed = True
                        yield ModelStreamEvent(
                            kind="usage",
                            request_id=request.request_id,
                            input_tokens=_nonnegative_int(payload.get("prompt_eval_count")),
                            output_tokens=_nonnegative_int(payload.get("eval_count")),
                            provider_request_id=provider_request_id,
                        )
                        yield ModelStreamEvent(
                            kind="done",
                            request_id=request.request_id,
                            finish_reason=str(payload.get("done_reason") or "stop"),
                            provider_request_id=provider_request_id,
                        )
        except ModelError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            error = classify_transport_error(exc, self.name, request.preferred_model)
            if error.code in {ModelErrorCode.NETWORK, ModelErrorCode.TIMEOUT}:
                error.code = ModelErrorCode.STREAM_INTERRUPTED
            raise error from exc
        if not completed:
            raise ModelError(
                ModelErrorCode.STREAM_INTERRUPTED,
                "Ollama stream ended before done event",
                True,
                self.name,
                request.preferred_model,
            )

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict:
        return {
            "model": request.preferred_model,
            "messages": [message.model_dump() for message in request.messages],
            "stream": stream,
            "options": {
                "temperature": self.temperature,
                "num_predict": request.max_output_tokens,
            },
        }


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        *,
        temperature: float = 0.35,
    ):
        self.http_client = http_client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature

    async def complete(self, request: ModelRequest) -> ModelResult:
        started = time.perf_counter()
        try:
            response = await self.http_client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(request, stream=False),
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise classify_transport_error(
                exc,
                self.name,
                request.preferred_model,
            ) from exc
        if response.is_error:
            raise classify_http_error(response, self.name, request.preferred_model)
        payload = _response_json(response, self.name, request.preferred_model)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _invalid_response(self.name, request.preferred_model, "missing choices[0]")
        first = choices[0]
        message = first.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise _invalid_response(self.name, request.preferred_model, "missing choices[0].message.content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        finish_reason = str(first.get("finish_reason") or "stop")
        return ModelResult(
            text=str(message["content"] or ""),
            provider=self.name,
            model=request.preferred_model,
            finish_reason=finish_reason,
            input_tokens=_nonnegative_int(usage.get("prompt_tokens")),
            output_tokens=_nonnegative_int(usage.get("completion_tokens")),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            request_id=request.request_id,
            provider_request_id=response.headers.get("x-request-id", ""),
            partial=_is_truncated(finish_reason),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        completed = False
        try:
            async with self.http_client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=self._payload(request, stream=True),
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise classify_http_error(response, self.name, request.preferred_model)
                provider_request_id = response.headers.get("x-request-id", "")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ").strip()
                    if raw == "[DONE]":
                        completed = True
                        break
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise _invalid_response(self.name, request.preferred_model, "invalid stream JSON") from exc
                    choices = payload.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        first = choices[0]
                        delta = first.get("delta") or {}
                        token = delta.get("content", "") if isinstance(delta, dict) else ""
                        if token:
                            yield ModelStreamEvent(
                                kind="token",
                                request_id=request.request_id,
                                text=str(token),
                                provider_request_id=provider_request_id,
                            )
                        finish_reason = first.get("finish_reason")
                        if finish_reason:
                            yield ModelStreamEvent(
                                kind="done",
                                request_id=request.request_id,
                                finish_reason=str(finish_reason),
                                provider_request_id=provider_request_id,
                            )
                    usage = payload.get("usage")
                    if isinstance(usage, dict):
                        yield ModelStreamEvent(
                            kind="usage",
                            request_id=request.request_id,
                            input_tokens=_nonnegative_int(usage.get("prompt_tokens")),
                            output_tokens=_nonnegative_int(usage.get("completion_tokens")),
                            provider_request_id=provider_request_id,
                        )
        except ModelError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            error = classify_transport_error(exc, self.name, request.preferred_model)
            if error.code in {ModelErrorCode.NETWORK, ModelErrorCode.TIMEOUT}:
                error.code = ModelErrorCode.STREAM_INTERRUPTED
            raise error from exc
        if not completed:
            raise ModelError(
                ModelErrorCode.STREAM_INTERRUPTED,
                "OpenAI stream ended before DONE event",
                True,
                self.name,
                request.preferred_model,
            )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict:
        payload = {
            "model": request.preferred_model,
            "messages": [message.model_dump() for message in request.messages],
            "temperature": self.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload


def _response_json(response: httpx.Response, provider: str, model: str) -> dict:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise _invalid_response(provider, model, "response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise _invalid_response(provider, model, "response JSON is not an object")
    return payload


def _invalid_response(provider: str, model: str, message: str) -> ModelError:
    return ModelError(
        ModelErrorCode.INVALID_RESPONSE,
        message,
        False,
        provider,
        model,
    )


def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_truncated(finish_reason: str) -> bool:
    return finish_reason.strip().lower() in {"length", "max_tokens", "max_output_tokens"}

