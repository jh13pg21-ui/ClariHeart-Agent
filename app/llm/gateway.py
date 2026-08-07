"""统一模型路由、隐私守卫与 Provider 级恢复入口。"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from dataclasses import replace
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from app.core.enums import RiskLevel
from app.llm.capabilities import ModelCapabilitiesRegistry
from app.llm.contracts import ModelRequest, ModelResult, ModelStreamEvent
from app.llm.egress import CloudEgressPolicy
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.providers import ModelProvider
from app.llm.recovery import (
    EventCallback,
    RecoveryEvent,
    RecoveryOrchestrator,
    RecoveryPolicy,
    Sleep,
)
from app.services.model_trace import (
    ModelTraceEvent,
    ModelTraceSink,
    NullModelTraceSink,
)


_FALLBACK_CODES = frozenset(
    {
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.OVERLOADED,
        ModelErrorCode.TIMEOUT,
        ModelErrorCode.NETWORK,
    }
)
_STREAM_RETRY_CODES = _FALLBACK_CODES | {ModelErrorCode.STREAM_INTERRUPTED}

StreamReviewer = Callable[[str, ModelRequest], bool | Awaitable[bool]]
ReplaceCallback = Callable[[str], Any | Awaitable[Any]]


class ModelGateway:
    """在一个共享 deadline 内完成本地优先、受控云降级调用。"""

    def __init__(
        self,
        providers: Mapping[str, ModelProvider],
        *,
        cloud_provider: str = "openai",
        cloud_model: str = "",
        capabilities_registry: ModelCapabilitiesRegistry | None = None,
        egress_policy: CloudEgressPolicy | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        stream_release_chars: int = 256,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        on_event: EventCallback | None = None,
        trace_sink: ModelTraceSink | None = None,
    ) -> None:
        self.providers = {
            str(name).strip().lower(): provider for name, provider in providers.items()
        }
        self.cloud_provider = str(cloud_provider).strip().lower()
        self.cloud_model = str(cloud_model).strip()
        self.capabilities_registry = capabilities_registry
        self.egress_policy = egress_policy or CloudEgressPolicy()
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        if stream_release_chars <= 0:
            raise ValueError("stream_release_chars 必须大于 0")
        self.stream_release_chars = stream_release_chars
        self._sleep = sleep
        self._clock = clock
        self._random = random_source
        self._on_event = on_event
        self._trace_sink = trace_sink or NullModelTraceSink()
        self._active_requests: dict[str, ModelRequest] = {}

    async def complete(self, request: ModelRequest) -> ModelResult:
        started = self._clock()
        self._active_requests[request.request_id] = request
        await self._trace(
            self._trace_event(
                "start",
                request,
                provider=request.preferred_provider,
                model=request.preferred_model,
                route="primary",
            )
        )
        try:
            result = await self._complete_routed(request)
        except ModelError as error:
            await self._trace(
                self._trace_event(
                    "failure",
                    request,
                    provider=error.provider,
                    model=error.model,
                    route=(
                        "cloud_fallback"
                        if error.provider == self.cloud_provider
                        else "primary"
                    ),
                    error_code=error.code.value,
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                    attempt=error.attempt,
                )
            )
            raise
        else:
            await self._trace(
                self._trace_event(
                    "success",
                    request,
                    provider=result.provider,
                    model=result.model,
                    route=(
                        "cloud_fallback"
                        if result.provider == self.cloud_provider
                        else "primary"
                    ),
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    latency_ms=max(
                        float(result.latency_ms or 0.0),
                        max(0.0, (self._clock() - started) * 1000),
                    ),
                )
            )
            return result
        finally:
            self._active_requests.pop(request.request_id, None)

    async def _complete_routed(self, request: ModelRequest) -> ModelResult:
        provider_name = request.preferred_provider.strip().lower()
        provider = self.providers.get(provider_name)
        if provider is None:
            raise self._route_error(
                request,
                ModelErrorCode.PERMANENT,
                f"模型 Provider 不可用: {provider_name or '<empty>'}",
            )

        if provider_name == self.cloud_provider:
            self._require_cloud_egress(request)

        deadline = self._clock() + self.recovery_policy.deadline_seconds
        try:
            return await self._complete_on(provider, request, deadline)
        except ModelError as primary_error:
            if not self._can_fallback(request, provider_name, primary_error):
                raise

            decision = self.egress_policy.evaluate(request)
            cloud = self.providers.get(self.cloud_provider)
            if not decision.allowed or cloud is None or not self.cloud_model:
                await self._emit(
                    RecoveryEvent(
                        kind="route_fallback_denied",
                        request_id=request.request_id,
                        provider=provider_name,
                        model=request.preferred_model,
                        attempt=primary_error.attempt,
                        error_code=primary_error.code.value,
                    )
                )
                raise

            cloud_request = replace(
                request,
                preferred_provider=self.cloud_provider,
                preferred_model=self.cloud_model,
            )
            await self._emit(
                RecoveryEvent(
                    kind="route_fallback",
                    request_id=request.request_id,
                    provider=self.cloud_provider,
                    model=self.cloud_model,
                    attempt=primary_error.attempt,
                    error_code=primary_error.code.value,
                )
            )
            return await self._complete_on(cloud, cloud_request, deadline)

    async def stream(
        self,
        request: ModelRequest,
        *,
        reviewer: StreamReviewer | None = None,
        on_replace: ReplaceCallback | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        started = self._clock()
        self._active_requests[request.request_id] = request
        await self._trace(
            self._trace_event(
                "stream_start",
                request,
                provider=request.preferred_provider,
                model=request.preferred_model,
                route="primary",
            )
        )
        input_tokens = output_tokens = 0
        try:
            async for event in self._stream_routed(
                request,
                reviewer=reviewer,
                on_replace=on_replace,
            ):
                input_tokens = max(input_tokens, int(event.input_tokens or 0))
                output_tokens = max(output_tokens, int(event.output_tokens or 0))
                yield event
        except ModelError as error:
            await self._trace(
                self._trace_event(
                    "stream_failure",
                    request,
                    provider=error.provider,
                    model=error.model,
                    route=(
                        "cloud_fallback"
                        if error.provider == self.cloud_provider
                        else "primary"
                    ),
                    error_code=error.code.value,
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                    attempt=error.attempt,
                )
            )
            raise
        else:
            await self._trace(
                self._trace_event(
                    "stream_success",
                    request,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                )
            )
        finally:
            self._active_requests.pop(request.request_id, None)

    async def _stream_routed(
        self,
        request: ModelRequest,
        *,
        reviewer: StreamReviewer | None = None,
        on_replace: ReplaceCallback | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        """流式输出；HIGH 全量缓冲，LOW/MEDIUM 按已审阅片段释放。"""

        provider_name = request.preferred_provider.strip().lower()
        provider = self.providers.get(provider_name)
        if provider is None:
            raise self._route_error(
                request,
                ModelErrorCode.PERMANENT,
                f"模型 Provider 不可用: {provider_name or '<empty>'}",
            )
        if provider_name == self.cloud_provider:
            self._require_cloud_egress(request)

        deadline = self._clock() + self.recovery_policy.deadline_seconds
        pending_tokens: list[ModelStreamEvent] = []
        terminal_events: list[ModelStreamEvent] = []
        full_text = ""
        released = False

        try:
            async for event in provider.stream(request):
                if event.kind == "token":
                    pending_tokens.append(event)
                    full_text += event.text
                    pending_text = "".join(item.text for item in pending_tokens)
                    if (
                        request.risk_level is not RiskLevel.HIGH
                        and reviewer is not None
                        and len(pending_text) >= self.stream_release_chars
                    ):
                        await self._require_stream_review(pending_text, request, reviewer)
                        for buffered in pending_tokens:
                            yield buffered
                        pending_tokens.clear()
                        released = True
                else:
                    terminal_events.append(event)
            if not any(event.kind == "done" for event in terminal_events):
                raise self._stream_interrupted(request, "流在 done 事件前结束")
        except ModelError as error:
            if error.code not in _STREAM_RETRY_CODES or not error.retryable:
                raise
            recovered = await self._recover_buffered_stream(
                request,
                provider_name,
                deadline,
                retries=self.recovery_policy.max_stream_retries,
                allow_fallback=True,
                last_error=error,
            )
            recovered_text = self._stream_text(recovered)
            await self._require_stream_review(recovered_text, request, reviewer)
            if released:
                if on_replace is not None:
                    await self._maybe_await(on_replace(recovered_text))
                yield ModelStreamEvent(
                    kind="replace_required",
                    request_id=request.request_id,
                    text=recovered_text,
                )
                for event in recovered:
                    if event.kind != "token":
                        yield event
            else:
                for event in recovered:
                    yield event
            return

        pending_text = "".join(item.text for item in pending_tokens)
        review_text = full_text if request.risk_level is RiskLevel.HIGH else pending_text
        await self._require_stream_review(review_text, request, reviewer)
        for event in pending_tokens:
            yield event
        for event in terminal_events:
            yield event

    async def _recover_buffered_stream(
        self,
        request: ModelRequest,
        provider_name: str,
        deadline: float,
        *,
        retries: int,
        allow_fallback: bool,
        last_error: ModelError,
    ) -> list[ModelStreamEvent]:
        provider = self.providers[provider_name]
        error = last_error
        for retry_number in range(1, retries + 1):
            if self._clock() >= deadline:
                raise error
            delay = self._stream_retry_delay(error, retry_number)
            if delay >= deadline - self._clock():
                raise error
            await self._emit(
                RecoveryEvent(
                    kind="stream_retry_scheduled",
                    request_id=request.request_id,
                    provider=provider_name,
                    model=request.preferred_model,
                    attempt=retry_number,
                    error_code=error.code.value,
                    delay_seconds=delay,
                )
            )
            await self._maybe_await(self._sleep(delay))
            try:
                return await self._collect_stream(provider, request, deadline)
            except ModelError as caught:
                caught.attempt = retry_number + 1
                error = caught
                if caught.code not in _STREAM_RETRY_CODES or not caught.retryable:
                    raise

        if not allow_fallback or not self._stream_can_fallback(request, provider_name):
            raise error
        decision = self.egress_policy.evaluate(request)
        cloud = self.providers.get(self.cloud_provider)
        if not decision.allowed or cloud is None or not self.cloud_model:
            raise error

        cloud_request = replace(
            request,
            preferred_provider=self.cloud_provider,
            preferred_model=self.cloud_model,
        )
        await self._emit(
            RecoveryEvent(
                kind="stream_route_fallback",
                request_id=request.request_id,
                provider=self.cloud_provider,
                model=self.cloud_model,
                attempt=error.attempt,
                error_code=error.code.value,
            )
        )
        try:
            return await self._collect_stream(cloud, cloud_request, deadline)
        except ModelError as cloud_error:
            if (
                cloud_error.code not in _STREAM_RETRY_CODES
                or not cloud_error.retryable
                or self.recovery_policy.max_stream_retries == 0
            ):
                raise
            return await self._recover_buffered_stream(
                cloud_request,
                self.cloud_provider,
                deadline,
                retries=self.recovery_policy.max_stream_retries,
                allow_fallback=False,
                last_error=cloud_error,
            )

    async def _collect_stream(
        self,
        provider: ModelProvider,
        request: ModelRequest,
        deadline: float,
    ) -> list[ModelStreamEvent]:
        if self._clock() >= deadline:
            raise ModelError(
                code=ModelErrorCode.TIMEOUT,
                message="流式模型调用 deadline 已耗尽",
                retryable=True,
                provider=request.preferred_provider,
                model=request.preferred_model,
            )
        events = [event async for event in provider.stream(request)]
        if not any(event.kind == "done" for event in events):
            raise self._stream_interrupted(request, "流在 done 事件前结束")
        return events

    async def _require_stream_review(
        self,
        text: str,
        request: ModelRequest,
        reviewer: StreamReviewer | None,
    ) -> None:
        if not text:
            return
        if reviewer is None:
            if request.risk_level is RiskLevel.HIGH:
                raise ModelError(
                    code=ModelErrorCode.CONTENT_POLICY,
                    message="HIGH 风险流式响应缺少完整安全审阅器",
                    retryable=False,
                    provider=request.preferred_provider,
                    model=request.preferred_model,
                )
            return
        approved = reviewer(text, request)
        if inspect.isawaitable(approved):
            approved = await approved
        if not approved:
            raise ModelError(
                code=ModelErrorCode.CONTENT_POLICY,
                message="流式响应未通过安全审阅",
                retryable=False,
                provider=request.preferred_provider,
                model=request.preferred_model,
            )

    def _stream_retry_delay(self, error: ModelError, retry_number: int) -> float:
        if error.retry_after_seconds is not None:
            return max(0.0, error.retry_after_seconds)
        base = min(
            self.recovery_policy.base_delay_seconds * (2 ** (retry_number - 1)),
            self.recovery_policy.maximum_delay_seconds,
        )
        jitter = base * self.recovery_policy.jitter_ratio * max(0.0, self._random())
        return min(base + jitter, self.recovery_policy.maximum_delay_seconds)

    def _stream_can_fallback(self, request: ModelRequest, provider_name: str) -> bool:
        return (
            request.risk_level is not RiskLevel.HIGH
            and provider_name != self.cloud_provider
        )

    @staticmethod
    def _stream_text(events: list[ModelStreamEvent]) -> str:
        return "".join(event.text for event in events if event.kind == "token")

    @staticmethod
    def _stream_interrupted(request: ModelRequest, message: str) -> ModelError:
        return ModelError(
            code=ModelErrorCode.STREAM_INTERRUPTED,
            message=message,
            retryable=True,
            provider=request.preferred_provider,
            model=request.preferred_model,
        )

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _complete_on(
        self,
        provider: ModelProvider,
        request: ModelRequest,
        deadline: float,
    ) -> ModelResult:
        orchestrator = RecoveryOrchestrator(
            provider,
            capabilities=(
                self.capabilities_registry.for_model(
                    request.preferred_provider,
                    request.preferred_model,
                )
                if self.capabilities_registry is not None
                else None
            ),
            policy=self.recovery_policy,
            sleep=self._sleep,
            clock=self._clock,
            random_source=self._random,
            on_event=self._emit,
        )
        return await orchestrator.complete(request, deadline_at=deadline)

    def _can_fallback(
        self,
        request: ModelRequest,
        provider_name: str,
        error: ModelError,
    ) -> bool:
        return (
            provider_name != self.cloud_provider
            and error.code in _FALLBACK_CODES
            and request.preferred_provider.strip().lower() != self.cloud_provider
        )

    def _require_cloud_egress(self, request: ModelRequest) -> None:
        decision = self.egress_policy.evaluate(request)
        if decision.allowed:
            return
        raise self._route_error(
            request,
            ModelErrorCode.CLOUD_EGRESS_DENIED,
            decision.reason,
        )

    @staticmethod
    def _route_error(
        request: ModelRequest,
        code: ModelErrorCode,
        message: str,
    ) -> ModelError:
        return ModelError(
            code=code,
            message=message,
            retryable=False,
            provider=request.preferred_provider,
            model=request.preferred_model,
        )

    async def _emit(self, event: RecoveryEvent) -> None:
        request = self._active_requests.get(event.request_id)
        if request is not None:
            await self._trace(
                self._trace_event(
                    event.kind,
                    request,
                    provider=event.provider,
                    model=event.model,
                    route=(
                        "cloud_fallback"
                        if event.provider == self.cloud_provider
                        else "primary"
                    ),
                    error_code=event.error_code,
                    attempt=event.attempt,
                )
            )
        if self._on_event is None:
            return
        result: Any = self._on_event(event)
        if inspect.isawaitable(result):
            await result

    async def _trace(self, event: ModelTraceEvent) -> None:
        try:
            result = self._trace_sink.record(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            return

    @staticmethod
    def _trace_event(
        kind: str,
        request: ModelRequest,
        **overrides,
    ) -> ModelTraceEvent:
        return ModelTraceEvent(
            kind=kind,
            request_id=request.request_id,
            agent_name=request.agent_name,
            task_name=request.task_name,
            provider=str(overrides.get("provider", request.preferred_provider)),
            model=str(overrides.get("model", request.preferred_model)),
            risk_level=request.risk_level.value,
            route=str(overrides.get("route", "primary")),
            error_code=str(overrides.get("error_code", "")),
            prompt_release=request.prompt_release,
            prompt_manifest_hash=request.prompt_manifest_hash,
            context_plan_hash=request.context_plan_hash,
            context_section_count=len(request.context_section_ids),
            input_tokens=int(overrides.get("input_tokens", 0) or 0),
            output_tokens=int(overrides.get("output_tokens", 0) or 0),
            latency_ms=float(overrides.get("latency_ms", 0.0) or 0.0),
            attempt=int(overrides.get("attempt", 1) or 0),
            cloud_egress=str(overrides.get("provider", request.preferred_provider)).lower()
            == "openai",
        )
