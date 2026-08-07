"""统一模型路由、隐私守卫与 Provider 级恢复入口。"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from dataclasses import replace
from typing import Any, Callable, Mapping

from app.llm.contracts import ModelRequest, ModelResult
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


_FALLBACK_CODES = frozenset(
    {
        ModelErrorCode.RATE_LIMITED,
        ModelErrorCode.OVERLOADED,
        ModelErrorCode.TIMEOUT,
        ModelErrorCode.NETWORK,
    }
)


class ModelGateway:
    """在一个共享 deadline 内完成本地优先、受控云降级调用。"""

    def __init__(
        self,
        providers: Mapping[str, ModelProvider],
        *,
        cloud_provider: str = "openai",
        cloud_model: str = "",
        egress_policy: CloudEgressPolicy | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        random_source: Callable[[], float] = random.random,
        on_event: EventCallback | None = None,
    ) -> None:
        self.providers = {
            str(name).strip().lower(): provider for name, provider in providers.items()
        }
        self.cloud_provider = str(cloud_provider).strip().lower()
        self.cloud_model = str(cloud_model).strip()
        self.egress_policy = egress_policy or CloudEgressPolicy()
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._random = random_source
        self._on_event = on_event

    async def complete(self, request: ModelRequest) -> ModelResult:
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

    async def _complete_on(
        self,
        provider: ModelProvider,
        request: ModelRequest,
        deadline: float,
    ) -> ModelResult:
        orchestrator = RecoveryOrchestrator(
            provider,
            policy=self.recovery_policy,
            sleep=self._sleep,
            clock=self._clock,
            random_source=self._random,
            on_event=self._on_event,
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
        if self._on_event is None:
            return
        result: Any = self._on_event(event)
        if inspect.isawaitable(result):
            await result
