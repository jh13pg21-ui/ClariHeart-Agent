from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.enums import EmotionLabel, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.risk_rules import detect_risk_signal


@dataclass
class PsychologyAssessment:
    emotion: EmotionLabel
    emotion_score: float
    risk: RiskLevel
    confidence: float
    summary: str


class PsychologicalAssessmentService:
    def __init__(self, ai: AiClient):
        self.ai = ai

    async def assess(self, text: str, history: list[AiMessage] | None = None) -> PsychologyAssessment:
        rule = detect_risk_signal(text)
        if rule.level == RiskLevel.HIGH:
            return PsychologyAssessment(EmotionLabel.HIGH_RISK, 4.0, RiskLevel.HIGH, 0.97, rule.reason)
        try:
            raw = await self.ai.complete(PromptTemplates.psychology_prompt(history or [], text))
            start = raw.find("{")
            end = raw.rfind("}")
            data = json.loads(raw[start:end + 1] if start >= 0 and end > start else raw)
            emotion = EmotionLabel(data.get("emotion", "NORMAL").upper())
            score = float(data.get("emotionScore", score_for_emotion(emotion)))
            risk = RiskLevel(data.get("risk", risk_from_score(score).value).upper())
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.75))))
            score_risk = risk_from_score(score)
            if risk_order(score_risk) > risk_order(risk):
                risk = score_risk
            if emotion == EmotionLabel.HIGH_RISK:
                risk = RiskLevel.HIGH
            if rule.level == RiskLevel.MEDIUM and risk == RiskLevel.LOW:
                risk = RiskLevel.MEDIUM
            return PsychologyAssessment(emotion, score, risk, confidence, data.get("summary", "模型评估结果"))
        except Exception:
            fallback = heuristic(text)
            if rule.level == RiskLevel.MEDIUM and fallback.risk == RiskLevel.LOW:
                return PsychologyAssessment(
                    fallback.emotion,
                    max(fallback.emotion_score, 3.0),
                    RiskLevel.MEDIUM,
                    0.82,
                    rule.reason,
                )
            return fallback


def heuristic(text: str) -> PsychologyAssessment:
    if has_consult_signal(text):
        if any(word in text.lower() for word in ["抑郁", "低落", "崩溃", "难过", "depress", "hopeless"]):
            return PsychologyAssessment(EmotionLabel.DEPRESSED, 3.1, RiskLevel.MEDIUM, 0.75, "检测到低落或抑郁相关表达")
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.2, RiskLevel.LOW, 0.72, "检测到焦虑或压力相关表达")
    return PsychologyAssessment(EmotionLabel.NORMAL, 0.0, RiskLevel.LOW, 0.66, "未检测到明显风险信号")


def score_for_emotion(emotion: EmotionLabel) -> float:
    return {
        EmotionLabel.HIGH_RISK: 4.0,
        EmotionLabel.DEPRESSED: 3.0,
        EmotionLabel.ANXIETY: 2.0,
        EmotionLabel.NORMAL: 0.0,
    }[emotion]


def risk_from_score(score: float) -> RiskLevel:
    if score >= 4:
        return RiskLevel.HIGH
    if score >= 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def risk_order(risk: RiskLevel) -> int:
    return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[risk]
