"""模型云端出站的隐私边界。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest


@dataclass(frozen=True)
class CloudEgressDecision:
    """不携带原始上下文的出站判定。"""

    allowed: bool
    reason: str
    permitted_section_ids: tuple[str, ...] = ()


class CloudEgressPolicy:
    """以失败关闭方式执行风险、授权和脱敏检查。"""

    def evaluate(self, request: ModelRequest) -> CloudEgressDecision:
        if request.risk_level is RiskLevel.HIGH:
            return self._deny("high_risk_must_remain_local")
        if request.risk_level not in {RiskLevel.LOW, RiskLevel.MEDIUM}:
            return self._deny("unsupported_risk_level")
        if not request.cloud_egress_allowed:
            return self._deny("cloud_egress_not_authorized")
        if not request.sanitized:
            return self._deny("request_not_sanitized")
        if not self._valid_section_manifest(request.context_section_ids):
            return self._deny("invalid_context_section_manifest")
        return CloudEgressDecision(
            allowed=True,
            reason="cloud_egress_allowed",
            permitted_section_ids=request.context_section_ids,
        )

    @staticmethod
    def _valid_section_manifest(section_ids: tuple[str, ...]) -> bool:
        normalized = tuple(section_id.strip() for section_id in section_ids)
        return all(normalized) and len(normalized) == len(set(normalized))

    @staticmethod
    def _deny(reason: str) -> CloudEgressDecision:
        return CloudEgressDecision(allowed=False, reason=reason)
