"""单次模型调用生命周期内的有界恢复。"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.llm.contracts import ModelRequest, ModelResult
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.providers import ModelProvider


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
    deadline_seconds: float = 20.0
    base_delay_seconds: float = 0.5
    maximum_delay_seconds: float = 32.0
    jitter_ratio: float = 0.25
    max_continuations: int = 2

    def __post_init__(self) -> None:
        if self.max_transient_retries < 0:
            raise ValueError("max_transient_retries 不能小于 0")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds 必须大于 0")
        if self.base_delay_seconds < 0 or self.maximum_delay_seconds < 0:
            raise ValueError("重试延迟不能为负数")
        if self.jitter_ratio < 0:
            raise ValueError("jitter_ratio 不能为负数")


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


class RecoveryOrchestrator:
    """执行 Provider 调用，并在 deadline 内处理可恢复瞬态错误。"""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        policy: RecoveryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        on_event: EventCallback | None = None,
    ) -> None:
        self.provider = provider
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
