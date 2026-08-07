"""可重复、无网络依赖的故障注入器与确定性降级策略。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping

from app.llm.contracts import ModelRequest, ModelResult, ModelStreamEvent
from app.llm.errors import ModelError, ModelErrorCode


@dataclass(frozen=True)
class StructuredFallback:
    value: Mapping[str, Any]
    degraded: bool
    reason: str = ""


def parse_structured_or_default(
    raw: str,
    *,
    default: Mapping[str, Any],
) -> StructuredFallback:
    """解析结构化结果；失败时返回调用方声明的安全默认值。"""

    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("根节点不是对象")
    except (json.JSONDecodeError, TypeError, ValueError):
        return StructuredFallback(dict(default), True, "invalid_json")
    return StructuredFallback(value, False)


def load_optional_cache(loader: Callable[[], Any], *, default: Any) -> tuple[Any, bool]:
    """缓存属于可选依赖；连接失败时显式进入降级态。"""

    try:
        return loader(), False
    except (ConnectionError, TimeoutError, OSError):
        return default, True


class FaultInjectingProvider:
    """只用于离线评测，不读取真实数据，也不访问真实 Provider。"""

    def __init__(self, provider: str, fault: str = "none") -> None:
        self.provider = provider
        self.fault = fault
        self.complete_calls = 0
        self.stream_calls = 0

    @property
    def calls(self) -> int:
        return self.complete_calls + self.stream_calls

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.complete_calls += 1
        if self.provider != "openai":
            error = self._completion_error()
            if error is not None:
                raise error
        text = "{not-json" if self.fault == "invalid_json" else '{"status":"ok"}'
        return ModelResult(
            text=text,
            provider=self.provider,
            model=request.preferred_model,
            finish_reason="stop",
            input_tokens=100,
            output_tokens=12,
            latency_ms=1.0,
            request_id=request.request_id,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.stream_calls += 1
        if (
            self.provider != "openai"
            and self.fault == "stream_interrupted"
            and self.stream_calls == 1
        ):
            yield ModelStreamEvent(kind="token", request_id=request.request_id, text="partial")
            raise ModelError(
                code=ModelErrorCode.STREAM_INTERRUPTED,
                message="injected stream interruption",
                retryable=True,
                provider=self.provider,
                model=request.preferred_model,
            )
        yield ModelStreamEvent(kind="token", request_id=request.request_id, text="recovered")
        yield ModelStreamEvent(
            kind="usage",
            request_id=request.request_id,
            input_tokens=100,
            output_tokens=12,
        )
        yield ModelStreamEvent(kind="done", request_id=request.request_id, finish_reason="stop")

    def _completion_error(self) -> ModelError | None:
        if self.fault == "prompt_too_long" and self.complete_calls == 1:
            return self._error(ModelErrorCode.PROMPT_TOO_LONG)
        code = {
            "429": ModelErrorCode.RATE_LIMITED,
            "529": ModelErrorCode.OVERLOADED,
            "timeout": ModelErrorCode.TIMEOUT,
        }.get(self.fault)
        return self._error(code) if code else None

    def _error(self, code: ModelErrorCode) -> ModelError:
        return ModelError(
            code=code,
            message=f"injected {code.value}",
            retryable=True,
            provider=self.provider,
            model="eval-local",
        )
