"""单次模型调用生命周期内的有界恢复。"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from app.llm.capabilities import ModelCapabilities
from app.llm.contracts import ModelRequest, ModelResult
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.providers import ModelProvider
from app.schemas.dtos import AiMessage


_TRANSIENT_CODES = frozenset(
    {
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.OVERLOADED,
        ModelErrorCode.TIMEOUT,
        ModelErrorCode.NETWORK,
    }
)


@dataclass(frozen=True)
class RecoveryPolicy:
    max_transient_retries: int = 2
    max_stream_retries: int = 1
    deadline_seconds: float = 20.0
    base_delay_seconds: float = 0.5
    maximum_delay_seconds: float = 32.0
    jitter_ratio: float = 0.25
    max_continuations: int = 2

    def __post_init__(self) -> None:
        if self.max_transient_retries < 0:
            raise ValueError("max_transient_retries 不能小于 0")
        if self.max_stream_retries < 0:
            raise ValueError("max_stream_retries 不能小于 0")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds 必须大于 0")
        if self.base_delay_seconds < 0 or self.maximum_delay_seconds < 0:
            raise ValueError("重试延迟不能为负数")
        if self.jitter_ratio < 0:
            raise ValueError("jitter_ratio 不能为负数")
        if self.max_continuations < 0:
            raise ValueError("max_continuations 不能小于 0")


@dataclass
class RecoveryState:
    attempt: int = 0
    transient_retries: int = 0
    output_escalations: int = 0
    continuations: int = 0
    reactive_compactions: int = 0
    consecutive_overloads: int = 0


@dataclass(frozen=True)
class RecoveryEvent:
    """可安全记录的恢复事件；刻意不包含 Prompt 或输出正文。"""

    kind: str
    request_id: str
    provider: str
    model: str
    attempt: int
    error_code: str = ""
    delay_seconds: float = 0.0


Sleep = Callable[[float], Awaitable[None] | Any]
EventCallback = Callable[[RecoveryEvent], Awaitable[None] | Any]

_CONTINUATION_INSTRUCTION = (
    "请只从上一段停止的位置继续，不要重复已经给出的内容，也不要重新写开头。"
    "保持原有格式，并优先补全尚未完成的关键结论。"
)


class RecoveryOrchestrator:
    """执行 Provider 调用，并在 deadline 内处理可恢复瞬态错误。"""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        capabilities: ModelCapabilities | None = None,
        policy: RecoveryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        on_event: EventCallback | None = None,
    ) -> None:
        self.provider = provider
        self.capabilities = capabilities
        self.policy = policy or RecoveryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._random = random_source
        self._on_event = on_event
        self._state: ContextVar[RecoveryState | None] = ContextVar(
            f"recovery_state_{id(self)}",
            default=None,
        )

    @property
    def last_state(self) -> RecoveryState:
        return self._state.get() or RecoveryState()

    async def complete(
        self,
        request: ModelRequest,
        *,
        deadline_at: float | None = None,
    ) -> ModelResult:
        state = RecoveryState()
        self._state.set(state)
        deadline = (
            deadline_at
            if deadline_at is not None
            else self._clock() + self.policy.deadline_seconds
        )
        current_request = request
        results: list[ModelResult] = []
        accumulated_text = ""
        previous_continuation = ""

        while True:
            result = await self._complete_once(current_request, deadline, state)
            results.append(result)
            if not self._is_truncated(result):
                final_text = accumulated_text + result.text if accumulated_text else result.text
                return self._aggregate(results, final_text, result.finish_reason, False)

            maximum_output = self._maximum_output_tokens(current_request)
            if (
                not accumulated_text
                and state.output_escalations == 0
                and current_request.max_output_tokens < maximum_output
            ):
                state.output_escalations += 1
                escalated_output = min(
                    maximum_output,
                    max(
                        current_request.max_output_tokens * 2,
                        current_request.max_output_tokens + 512,
                    ),
                )
                current_request = replace(
                    request,
                    max_output_tokens=escalated_output,
                )
                await self._emit(
                    RecoveryEvent(
                        kind="output_budget_escalated",
                        request_id=request.request_id,
                        provider=request.preferred_provider,
                        model=request.preferred_model,
                        attempt=state.attempt,
                    )
                )
                continue

            if not accumulated_text:
                accumulated_text = result.text
            else:
                continuation = result.text
                repeated = self._similarity(previous_continuation, continuation) >= 0.90
                if not repeated:
                    accumulated_text += continuation
                if repeated or len(self._normalize(continuation)) < 128:
                    return self._aggregate(
                        results,
                        accumulated_text,
                        "recovery_limit",
                        True,
                    )
                previous_continuation = continuation

            if state.continuations >= self.policy.max_continuations:
                return self._aggregate(
                    results,
                    accumulated_text,
                    "recovery_limit",
                    True,
                )

            state.continuations += 1
            current_request = replace(
                request,
                max_output_tokens=maximum_output,
                messages=request.messages
                + (
                    AiMessage(role="assistant", content=accumulated_text),
                    AiMessage(role="user", content=_CONTINUATION_INSTRUCTION),
                ),
            )
            await self._emit(
                RecoveryEvent(
                    kind="output_continuation",
                    request_id=request.request_id,
                    provider=request.preferred_provider,
                    model=request.preferred_model,
                    attempt=state.attempt,
                )
            )

    async def _complete_once(
        self,
        request: ModelRequest,
        deadline: float,
        state: RecoveryState,
    ) -> ModelResult:
        last_error: ModelError | None = None

        while True:
            if self._clock() >= deadline:
                if last_error is not None:
                    raise last_error
                raise ModelError(
                    code=ModelErrorCode.TIMEOUT,
                    message="模型调用 deadline 已耗尽",
                    retryable=True,
                    provider=request.preferred_provider,
                    model=request.preferred_model,
                    attempt=state.attempt,
                )
            state.attempt += 1
            try:
                result = await self.provider.complete(request)
            except ModelError as error:
                last_error = error
                error.attempt = state.attempt
                if error.code is ModelErrorCode.OVERLOADED:
                    state.consecutive_overloads += 1
                else:
                    state.consecutive_overloads = 0
                if not self._can_retry(error, state):
                    raise

                delay = self._retry_delay(error, state.transient_retries + 1)
                remaining = deadline - self._clock()
                if delay >= remaining:
                    raise

                state.transient_retries += 1
                await self._emit(
                    RecoveryEvent(
                        kind="retry_scheduled",
                        request_id=request.request_id,
                        provider=error.provider,
                        model=error.model,
                        attempt=state.attempt,
                        error_code=error.code.value,
                        delay_seconds=delay,
                    )
                )
                await self._maybe_await(self._sleep(delay))
                continue

            state.consecutive_overloads = 0
            return result

    def _maximum_output_tokens(self, request: ModelRequest) -> int:
        if self.capabilities is None:
            return request.max_output_tokens
        return max(request.max_output_tokens, self.capabilities.maximum_output_tokens)

    @staticmethod
    def _is_truncated(result: ModelResult) -> bool:
        reason = result.finish_reason.strip().lower()
        return result.partial or reason in {"length", "max_tokens", "max_output_tokens"}

    @staticmethod
    def _normalize(text: str) -> str:
        return "".join(str(text or "").split()).lower()

    @classmethod
    def _similarity(cls, previous: str, current: str) -> float:
        left = cls._normalize(previous)
        right = cls._normalize(current)
        if not left or not right:
            return 0.0
        return difflib.SequenceMatcher(None, left, right).ratio()

    @staticmethod
    def _aggregate(
        results: list[ModelResult],
        text: str,
        finish_reason: str,
        partial: bool,
    ) -> ModelResult:
        last = results[-1]
        metadata = last.metadata + (
            ("recovery_call_count", len(results)),
            ("recovery_partial", partial),
        )
        return replace(
            last,
            text=text,
            finish_reason=finish_reason,
            input_tokens=sum(item.input_tokens for item in results),
            output_tokens=sum(item.output_tokens for item in results),
            latency_ms=sum(item.latency_ms for item in results),
            partial=partial,
            metadata=metadata,
        )

    def _can_retry(self, error: ModelError, state: RecoveryState) -> bool:
        return (
            error.retryable
            and error.code in _TRANSIENT_CODES
            and state.transient_retries < self.policy.max_transient_retries
        )

    def _retry_delay(self, error: ModelError, retry_number: int) -> float:
        if error.retry_after_seconds is not None:
            return max(0.0, error.retry_after_seconds)
        base = min(
            self.policy.base_delay_seconds * (2 ** (retry_number - 1)),
            self.policy.maximum_delay_seconds,
        )
        jitter = base * self.policy.jitter_ratio * max(0.0, self._random())
        return min(base + jitter, self.policy.maximum_delay_seconds)

    async def _emit(self, event: RecoveryEvent) -> None:
        if self._on_event is not None:
            await self._maybe_await(self._on_event(event))

    @staticmethod
    async def _maybe_await(value: Any) -> None:
        if inspect.isawaitable(value):
            await value
