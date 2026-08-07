from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator, Iterable

import httpx

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.context.contracts import ContextEnvelope, ContextSection
from app.context.planner import ContextPlanner
from app.context.tokens import TokenEstimatorRegistry
from app.llm.capabilities import ModelCapabilitiesRegistry
from app.llm.contracts import ModelRequest, ModelResult, ModelStreamEvent
from app.llm.gateway import ModelGateway, ReplaceCallback, StreamReviewer
from app.llm.providers import OllamaProvider, OpenAICompatibleProvider
from app.llm.recovery import RecoveryPolicy
from app.prompts.assembler import PromptAssembler, PromptRequest
from app.prompts.registry import default_prompt_registry
from app.prompts.release import PROMPT_RELEASE
from app.prompts.runtime import registered_task_messages, render_registered_prefix
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer
from app.services.risk_rules import detect_risk_signal
from app.services.model_trace import ModelTraceSink


class PromptTemplates:
    @staticmethod
    def intent_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return registered_task_messages(
            "agent.understanding",
            "task.intent_classification",
            {
                "recentHistory": [message.model_dump() for message in history[-20:]],
                "currentInput": user_input,
            },
        )

    @staticmethod
    def psychology_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return registered_task_messages(
            "agent.safety",
            "task.risk_assessment",
            {
                "recentHistory": [message.model_dump() for message in history[-20:]],
                "currentInput": user_input,
            },
        )

    @staticmethod
    def answer_system_prompt(intent: IntentType, risk: RiskLevel, context: str, display_name: str, skill_context: str = "") -> AiMessage:
        mode = "normal_chat" if intent == IntentType.CHAT and risk == RiskLevel.LOW else "support"
        prefix = render_registered_prefix(
            "agent.response",
            "task.response_generation",
            mode=mode,
        )
        attachment = {
            "trust": "UNTRUSTED_DATA",
            "displayName": display_name,
            "retrievedKnowledge": context,
            "skillContext": skill_context,
            "intent": intent.value,
            "risk": risk.value,
        }
        return AiMessage(
            role="system",
            content=prefix + "\n\nUNTRUSTED_DATA\n" + json.dumps(
                attachment,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


class _MockModelProvider:
    name = "mock"

    def __init__(self, responder):
        self._responder = responder

    async def complete(self, request: ModelRequest) -> ModelResult:
        await asyncio.sleep(0)
        text = self._responder(list(request.messages))
        return ModelResult(
            text=text,
            provider=self.name,
            model=request.preferred_model,
            finish_reason="stop",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            request_id=request.request_id,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        result = await self.complete(request)
        for chunk in split_text(result.text, 12):
            yield ModelStreamEvent(kind="token", request_id=request.request_id, text=chunk)
        yield ModelStreamEvent(kind="usage", request_id=request.request_id)
        yield ModelStreamEvent(
            kind="done",
            request_id=request.request_id,
            finish_reason="stop",
        )


class AiClient:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        *,
        gateway: ModelGateway | None = None,
        trace_sink: ModelTraceSink | None = None,
    ):
        self.settings = settings
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0, read=45.0, write=15.0, pool=5.0)
        )
        self._capabilities_registry = ModelCapabilitiesRegistry(self.settings)
        self._estimator_registry = TokenEstimatorRegistry(self.settings)
        self._prompt_registry = default_prompt_registry()
        self._trace_sink = trace_sink
        self.gateway = gateway or self._build_gateway()
        self._sanitizer = PrivacySanitizer()

    async def complete(
        self,
        messages: list[AiMessage],
        **metadata,
    ) -> str:
        return (await self.complete_result(messages, **metadata)).text

    async def complete_result(
        self,
        messages: list[AiMessage],
        *,
        agent_name: str = "legacy",
        task_name: str = "legacy",
        risk_level: RiskLevel = RiskLevel.LOW,
        cloud_egress_allowed: bool = False,
        context_section_ids: tuple[str, ...] = (),
        output_schema_id: str = "",
        prompt_manifest_hash: str = "",
        context_plan_hash: str = "",
        prompt_release: str = PROMPT_RELEASE,
    ) -> ModelResult:
        request = self._request(
            messages,
            agent_name=agent_name,
            task_name=task_name,
            risk_level=risk_level,
            cloud_egress_allowed=cloud_egress_allowed,
            context_section_ids=context_section_ids,
            output_schema_id=output_schema_id,
            prompt_manifest_hash=prompt_manifest_hash,
            context_plan_hash=context_plan_hash,
            prompt_release=prompt_release,
            stream=False,
        )
        return await self.gateway.complete(request)

    async def complete_registered_task(
        self,
        *,
        agent_name: str,
        agent_prompt_id: str,
        task_name: str,
        payload,
        risk_level: RiskLevel = RiskLevel.LOW,
        mode: str = "default",
        locale: str = "zh-CN",
    ) -> ModelResult:
        provider = self._provider_name()
        model = self._model_name(provider)
        capabilities = self._capabilities_registry.for_model(provider, model)
        estimator = self._estimator_registry.for_model(capabilities)
        request_id = uuid.uuid4().hex
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        envelope = ContextEnvelope(
            request_id=request_id,
            agent_name=agent_name,
            task_name=task_name,
            risk_level=risk_level,
            sections=(
                ContextSection(
                    id="user.current",
                    category="user",
                    content=content,
                    required=True,
                    priority=100,
                    loading_reason="registered_task_payload",
                ),
            ),
        )
        planner = ContextPlanner(
            estimator,
            reserve_tokens=int(getattr(self.settings, "model_recovery_reserve_tokens", 1024)),
            margin_ratio=float(getattr(self.settings, "model_provider_safety_margin_ratio", 0.10)),
        )
        plan = planner.plan(
            envelope,
            capabilities,
            max(1, int(getattr(self.settings, "ai_max_tokens", 512))),
        )
        task_prompt_id = f"task.{task_name}"
        assembled = PromptAssembler(self._prompt_registry, estimator).assemble(
            PromptRequest(
                request_id=request_id,
                agent_name=agent_name,
                task_name=task_name,
                agent_prompt_id=agent_prompt_id,
                task_prompt_id=task_prompt_id,
                mode=mode,
                locale=locale,
            ),
            plan,
        )
        definition = self._prompt_registry.get(task_prompt_id)
        return await self.complete_result(
            list(assembled.messages),
            agent_name=agent_name,
            task_name=task_name,
            risk_level=risk_level,
            cloud_egress_allowed=risk_level is not RiskLevel.HIGH,
            context_section_ids=assembled.context_section_ids,
            output_schema_id=definition.output_schema_id,
            prompt_manifest_hash=assembled.manifest.manifest_hash,
            context_plan_hash=plan.plan_hash,
        )

    async def stream(
        self,
        messages: list[AiMessage],
        **metadata,
    ) -> AsyncIterator[str]:
        async for event in self.stream_events(messages, **metadata):
            if event.kind == "token" and event.text:
                yield event.text

    async def stream_events(
        self,
        messages: list[AiMessage],
        *,
        agent_name: str = "legacy",
        task_name: str = "legacy",
        risk_level: RiskLevel = RiskLevel.LOW,
        cloud_egress_allowed: bool = False,
        context_section_ids: tuple[str, ...] = (),
        output_schema_id: str = "",
        prompt_manifest_hash: str = "",
        context_plan_hash: str = "",
        prompt_release: str = PROMPT_RELEASE,
        reviewer: StreamReviewer | None = None,
        on_replace: ReplaceCallback | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        request = self._request(
            messages,
            agent_name=agent_name,
            task_name=task_name,
            risk_level=risk_level,
            cloud_egress_allowed=cloud_egress_allowed,
            context_section_ids=context_section_ids,
            output_schema_id=output_schema_id,
            prompt_manifest_hash=prompt_manifest_hash,
            context_plan_hash=context_plan_hash,
            prompt_release=prompt_release,
            stream=True,
        )
        async for event in self.gateway.stream(
            request,
            reviewer=reviewer,
            on_replace=on_replace,
        ):
            yield event

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    def _request(
        self,
        messages: list[AiMessage],
        *,
        agent_name: str,
        task_name: str,
        risk_level: RiskLevel,
        cloud_egress_allowed: bool,
        context_section_ids: tuple[str, ...],
        output_schema_id: str,
        prompt_manifest_hash: str,
        context_plan_hash: str,
        prompt_release: str,
        stream: bool,
    ) -> ModelRequest:
        provider = self._provider_name()
        configured_cloud = provider == "openai"
        fallback_enabled = bool(
            getattr(self.settings, "model_cloud_fallback_enabled", True)
        )
        allow_cloud = (
            risk_level is not RiskLevel.HIGH
            and (configured_cloud or (cloud_egress_allowed and fallback_enabled))
        )
        outbound_messages = tuple(messages)
        section_ids = tuple(context_section_ids)
        if allow_cloud:
            outbound_messages = tuple(
                AiMessage(role=message.role, content=self._sanitizer.sanitize(message.content))
                for message in messages
            )
            if not section_ids:
                section_ids = tuple(f"legacy-message-{index}" for index in range(len(messages)))
        return ModelRequest(
            request_id=uuid.uuid4().hex,
            agent_name=agent_name,
            task_name=task_name,
            risk_level=risk_level,
            messages=outbound_messages,
            preferred_provider=provider,
            preferred_model=self._model_name(provider),
            max_output_tokens=max(1, int(getattr(self.settings, "ai_max_tokens", 512))),
            stream=stream,
            output_schema_id=output_schema_id,
            prompt_manifest_hash=prompt_manifest_hash,
            cloud_egress_allowed=allow_cloud,
            sanitized=allow_cloud,
            context_section_ids=section_ids,
            context_plan_hash=context_plan_hash,
            prompt_release=prompt_release,
        )

    def _build_gateway(self) -> ModelGateway:
        providers = {}
        provider = self._provider_name()
        if provider == "mock":
            providers["mock"] = _MockModelProvider(self._mock)
        else:
            providers["ollama"] = OllamaProvider(
                self.http_client,
                str(getattr(self.settings, "ollama_base_url", "http://localhost:11434")),
                temperature=float(getattr(self.settings, "ai_temperature", 0.35)),
            )
        api_key = str(getattr(self.settings, "openai_api_key", "") or "").strip()
        if api_key:
            providers["openai"] = OpenAICompatibleProvider(
                self.http_client,
                str(getattr(self.settings, "openai_base_url", "https://api.openai.com/v1")),
                api_key,
                temperature=float(getattr(self.settings, "ai_temperature", 0.35)),
            )
        return ModelGateway(
            providers,
            cloud_provider="openai",
            cloud_model=str(getattr(self.settings, "openai_model", "")),
            capabilities_registry=self._capabilities_registry,
            recovery_policy=RecoveryPolicy(
                max_transient_retries=int(
                    getattr(self.settings, "model_recovery_max_transient_retries", 2)
                ),
                max_stream_retries=int(
                    getattr(self.settings, "model_recovery_max_stream_retries", 1)
                ),
                deadline_seconds=float(
                    getattr(self.settings, "model_recovery_deadline_seconds", 65.0)
                ),
                base_delay_seconds=float(
                    getattr(self.settings, "model_recovery_base_delay_seconds", 0.5)
                ),
                maximum_delay_seconds=float(
                    getattr(self.settings, "model_recovery_max_delay_seconds", 32.0)
                ),
                jitter_ratio=float(
                    getattr(self.settings, "model_recovery_jitter_ratio", 0.25)
                ),
                max_continuations=int(
                    getattr(self.settings, "model_recovery_max_continuations", 2)
                ),
            ),
            stream_release_chars=int(
                getattr(self.settings, "model_stream_release_chars", 256)
            ),
            trace_sink=self._trace_sink,
        )

    def _provider_name(self) -> str:
        provider = str(getattr(self.settings, "ai_provider", "mock")).strip().lower()
        return provider if provider in {"ollama", "openai"} else "mock"

    def _model_name(self, provider: str) -> str:
        if provider == "openai":
            return str(getattr(self.settings, "openai_model", ""))
        if provider == "ollama":
            return str(getattr(self.settings, "ollama_model", ""))
        return "mock"

    def _mock(self, messages: list[AiMessage]) -> str:
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        system = " ".join(m.content for m in messages if m.role == "system")
        if "严格 JSON" in system or "task.risk_assessment" in system:
            if has_high_risk_signal(last):
                return '{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.95,"summary":"检测到明确高风险表达"}'
            if has_consult_signal(last):
                return '{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","confidence":0.72,"summary":"检测到压力或情绪求助表达"}'
            return '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.66,"summary":"未检测到明显风险信号"}'
        if "意图分类器" in system or "task.intent_classification" in system:
            if has_high_risk_signal(last):
                return "RISK"
            if has_consult_signal(last):
                return "CONSULT"
            return "CHAT"
        if "high_risk_safety_plan" in system and has_high_risk_signal(last):
            return "我听到你现在已经痛苦到觉得撑不下去了。现在最重要的是先让你不要一个人扛：请马上联系身边可信任的人，或者直接联系辅导员、学校心理中心、校园保卫/当地紧急服务。接下来 10 分钟，请先把自己移到有人在的地方，并把可能伤害自己的东西放远一点。如果可以，回我一句：你现在身边有没有可以马上联系或走过去找的人？"
        if "当前由 ResponseAgent 以 support mode" in system:
            return "我听到你最近压力很大，还影响到了睡眠，这种状态确实会让人很消耗。你可以先做两件小事：今晚把最担心的事情写成清单，先只选一个最小步骤处理；睡前 30 分钟把手机和学习任务放远一点，用缓慢呼吸或热水澡帮身体降下来。如果这种失眠持续一周以上，建议联系学校心理中心或辅导员一起看一看。"
        if "当前由 ResponseAgent 以 normal_chat mode" in system:
            return "我在。这个问题可以直接拆开来看，我们先从你最想解决的那一部分开始。"
        if "ContextAgent" in system and "SUFFICIENT" in system:
            return "SUFFICIENT"
        if "ContextAgent" in system:
            return last[:40] or "校园心理支持"
        return "我在。先把你现在最具体的困扰说出来，我们可以一步一步拆开。如果情况已经影响安全，请马上联系身边可信任的人或学校心理中心。"


def format_history(history: list[AiMessage]) -> str:
    if not history:
        return "无"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-20:])


CONSULT_WORDS = ["焦虑", "抑郁", "压力", "失眠", "难过", "崩溃", "痛苦", "无助", "心理", "咨询", "anxious", "depress", "stress"]


def has_high_risk_signal(text: str) -> bool:
    return detect_risk_signal(text).level == RiskLevel.HIGH


def has_consult_signal(text: str) -> bool:
    normalized = text.lower()
    return any(word in normalized for word in CONSULT_WORDS)


def split_text(text: str, size: int) -> Iterable[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]
