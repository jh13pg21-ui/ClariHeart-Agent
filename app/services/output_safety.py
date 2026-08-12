from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

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
_DEPENDENCY_PATTERNS = (
    re.compile(r"(?:只有|只能)(?:我|这个机器人).{0,12}(?:理解|帮助|陪伴|拯救)你"),
    re.compile(r"不要告诉(?:家人|老师|辅导员|心理中心|任何人)"),
)
_DIAGNOSIS_PATTERNS = (
    re.compile(r"你(?:就是|肯定|一定|已经).{0,4}(?:得了|患有|是).{0,8}(?:抑郁症|焦虑症|双相|精神分裂)"),
    re.compile(r"我(?:可以|来)给你(?:确诊|诊断)"),
)
_MEDICATION_PATTERNS = (
    re.compile(r"(?:马上|直接|自行)(?:停药|减药|加药|换药)"),
    re.compile(r"(?:建议|应该)你服用.{0,12}(?:毫克|mg|片)"),
)
_SECRECY_PATTERNS = (
    re.compile(r"我保证.{0,8}(?:绝对|完全)?保密"),
    re.compile(r"不会告诉任何人"),
)
_SUSPICIOUS_HOTLINE = re.compile(r"(?:热线|急救|报警).{0,8}(?<!\d)(\d{5,12})(?!\d)")


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
    for patterns, reason in (
        (_DEPENDENCY_PATTERNS, "encourages emotional dependency or isolation"),
        (_DIAGNOSIS_PATTERNS, "unsupported diagnosis claim"),
        (_MEDICATION_PATTERNS, "unsafe medication instruction"),
        (_SECRECY_PATTERNS, "absolute confidentiality promise"),
    ):
        if any(pattern.search(candidate) for pattern in patterns):
            return OutputSafetyDecision(OutputSafetyStatus.REVISE, candidate, reason)
    hotline = _SUSPICIOUS_HOTLINE.search(candidate)
    if hotline and hotline.group(1) not in {"110", "119", "120"}:
        return OutputSafetyDecision(OutputSafetyStatus.REVISE, candidate, "unverified emergency hotline number")
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


def pop_reviewable_stream_segments(buffer: str) -> tuple[list[str], str]:
    """仅释放完整句子，避免把尚未完成安全审查的半句推送到前端。"""

    segments = []
    start = 0
    for index, char in enumerate(buffer):
        if char in "。！？!?\n":
            segments.append(buffer[start:index + 1])
            start = index + 1
    return [item for item in segments if item], buffer[start:]
