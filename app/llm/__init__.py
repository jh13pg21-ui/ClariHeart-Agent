"""统一模型调用、路由与恢复基础设施。"""

from app.llm.capabilities import ModelCapabilities, ModelCapabilitiesRegistry
from app.llm.contracts import ModelRequest, ModelResult, ModelStreamEvent
from app.llm.errors import ModelError, ModelErrorCode
from app.llm.egress import CloudEgressDecision, CloudEgressPolicy
from app.llm.providers import ModelProvider, OllamaProvider, OpenAICompatibleProvider
from app.llm.recovery import (
    RecoveryEvent,
    RecoveryOrchestrator,
    RecoveryPolicy,
    RecoveryState,
)

__all__ = [
    "ModelCapabilities",
    "ModelCapabilitiesRegistry",
    "ModelRequest",
    "ModelResult",
    "ModelStreamEvent",
    "ModelError",
    "ModelErrorCode",
    "CloudEgressDecision",
    "CloudEgressPolicy",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "RecoveryEvent",
    "RecoveryOrchestrator",
    "RecoveryPolicy",
    "RecoveryState",
]
