from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator, Iterable

import httpx

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.llm.capabilities import ModelCapabilitiesRegistry
from app.llm.contracts import ModelRequest, ModelResult, ModelStreamEvent
from app.llm.gateway import ModelGateway, ReplaceCallback, StreamReviewer
from app.llm.providers import OllamaProvider, OpenAICompatibleProvider
from app.llm.recovery import RecoveryPolicy
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer
from app.services.risk_rules import detect_risk_signal


class PromptTemplates:
    @staticmethod
    def intent_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你是一个用户意图分类器，只做意图识别，不回答问题。"
                "只输出 CHAT、CONSULT 之一。CHAT 包含普通闲聊、学习、编程、作业、校园事务；"
                "CONSULT 包含压力、焦虑、低落、失眠、情绪倾诉以及自杀、自残、伤人或即时危险表达。"
                "风险程度由独立 SafetyAgent 评估，不要输出 RISK。"
                "必须结合最近上下文理解“是、继续、安慰一下、然后呢”等省略或承接表达，"
                "不能因为当前输入很短就忽略上一轮正在讨论的主题。"
            )),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def psychology_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你负责分析校园心理健康消息。只返回严格 JSON："
                '{"emotion":"NORMAL|ANXIETY|DEPRESSED|HIGH_RISK","emotionScore":0.0,'
                '"risk":"LOW|MEDIUM|HIGH","confidence":0.0,"summary":"short reason"}。'
                "评估时必须结合最近上下文；对承接上一轮的短句，不得仅因当前文字缺少风险关键词就忽略尚未解决的风险。"
            )),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def answer_system_prompt(intent: IntentType, risk: RiskLevel, context: str, display_name: str, skill_context: str = "") -> AiMessage:
        if intent == IntentType.CHAT:
            content = (
                "你是 MindBridge，一个面向学生的日常陪伴与校园生活助手。"
                "普通学习、编程、校园事务和通用问题请自然、准确、直接地回答。"
                "不要主动做心理测评，不要输出风险等级、心理标签、诊断结论或报告口吻。"
                f"学生显示名：{display_name}"
            )
            return AiMessage(role="system", content=content)
        crisis_rule = ""
        if risk == RiskLevel.HIGH:
            crisis_rule = (
                "\n高风险处理规则：先回应情绪，再关注当前安全；鼓励用户立刻联系身边可信任的人、"
                "学校辅导员/心理中心或当地紧急救助；不提供任何危险操作细节。"
            )
        content = (
            "你是 MindBridge，一个面向学生的校园心理关怀智能体。"
            "回答要共情、谨慎、非评判，不诊断疾病，不开药，不替代持证心理咨询师。"
            "不要向学生输出风险等级、报告分数或后台标签。"
            "优先基于检索知识回答；知识不足时明确说明并给出安全通用建议。"
            f"\n学生显示名：{display_name}\n检索知识：\n{context}\n\n可用 skill 指引：\n{skill_context or '无'}{crisis_rule}"
        )
        return AiMessage(role="system", content=content)


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
    ):
        self.settings = settings
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0, read=45.0, write=15.0, pool=5.0)
        )
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
            stream=False,
        )
        return await self.gateway.complete(request)

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
            capabilities_registry=ModelCapabilitiesRegistry(self.settings),
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
        if "严格 JSON" in system:
            if has_high_risk_signal(last):
                return '{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.95,"summary":"检测到明确高风险表达"}'
            if has_consult_signal(last):
                return '{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","confidence":0.72,"summary":"检测到压力或情绪求助表达"}'
            return '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.66,"summary":"未检测到明显风险信号"}'
        if "意图分类器" in system:
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
