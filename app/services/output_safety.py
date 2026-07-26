from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.enums import RiskLevel


class OutputSafetyStatus(str, Enum):
    APPROVED = "APPROVED"
    REVISE = "REVISE"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True)
class OutputSafetyDecision:
    status: OutputSafetyStatus
    text: str
    reason: str


_LEAKED_LABELS = (
    "后台风险",
    "风险评分",
    "风险等级",
    "置信度",
    "emotionScore",
    "HIGH_RISK",
    "SAFETY_OVERRIDE",
)
_DANGEROUS_METHODS = (
    "割腕",
    "刀片",
    "寻找血管",
    "上吊",
    "服用剂量",
    "致死剂量",
    "跳楼方法",
    "自杀步骤",
)
_SUPPORT_TERMS = ("可信任的人", "陪着你", "身边的人", "辅导员", "心理中心")
_SAFETY_QUESTIONS = ("你现在安全吗", "有没有可以马上联系", "是否有立即危险", "身边有没有人")
_EMERGENCY_TERMS = ("紧急服务", "急救", "报警", "校园保卫", "当地紧急", "危机热线")


def safe_fallback(risk: RiskLevel) -> str:
    if risk == RiskLevel.HIGH:
        return (
            "我很在意你现在的安全，请先不要独自承受，马上联系一位身边可信任的人陪着你。"
            "你现在安全吗，身边有没有可以马上联系的人？"
            "如果你可能马上伤害自己或处于立即危险，请联系当地紧急服务、校园保卫或学校心理中心。"
        )
    return "抱歉，我现在无法安全地完成这条回复。你可以换一种方式告诉我你最需要的帮助。"


def review_output(text: str, risk: RiskLevel) -> OutputSafetyDecision:
    candidate = (text or "").strip()
    if not candidate:
        return OutputSafetyDecision(OutputSafetyStatus.FALLBACK, safe_fallback(risk), "empty output")
    if any(term.lower() in candidate.lower() for term in _DANGEROUS_METHODS):
        return OutputSafetyDecision(
            OutputSafetyStatus.FALLBACK,
            safe_fallback(risk),
            "dangerous self-harm method detail",
        )
    if any(term.lower() in candidate.lower() for term in _LEAKED_LABELS):
        return OutputSafetyDecision(OutputSafetyStatus.REVISE, candidate, "internal label or score leaked")
    if risk == RiskLevel.HIGH:
        has_support = any(term in candidate for term in _SUPPORT_TERMS)
        has_safety_question = any(term in candidate for term in _SAFETY_QUESTIONS)
        has_emergency_direction = any(term in candidate for term in _EMERGENCY_TERMS)
        if not (has_support and has_safety_question and has_emergency_direction):
            return OutputSafetyDecision(
                OutputSafetyStatus.REVISE,
                candidate,
                "high-risk output lacks support, current-safety question, or emergency direction",
            )
    return OutputSafetyDecision(OutputSafetyStatus.APPROVED, candidate, "approved")
