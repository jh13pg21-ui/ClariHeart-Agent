from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.enums import RiskLevel


@dataclass(frozen=True)
class RiskRuleResult:
    level: RiskLevel | None
    reason: str
    matched: str = ""


_HIGH_PATTERNS = (
    re.compile(r"(?:我|自己|本人).{0,16}(不想活|想死|自杀|自残|轻生|结束生命|结束自己的生命|伤害自己|割腕|跳楼|服药自尽)"),
    re.compile(r"(?:割腕|跳楼|服药).{0,8}(?:结束自己|结束生命)"),
    re.compile(r"(?:已经|正在|准备|打算|计划|今晚|现在|马上).{0,12}(自杀|自残|轻生|结束生命|割腕|跳楼|伤害自己|服药)"),
    re.compile(r"\b(?:i\s+(?:want|plan|intend)\s+to\s+(?:die|kill myself|end my life)|i.{0,32}(?:suicide|self[- ]harm)|kill myself|end my life)\b", re.I),
)
_DENIAL_PATTERNS = (
    re.compile(r"(?:我)?(?:没有|没想过|不会|并不|不打算|从未).{0,8}(自杀|轻生|伤害自己|想死)"),
    re.compile(r"\b(?:i\s+(?:do not|don't|would never)\s+(?:want to\s+)?(?:die|kill myself)|not suicidal)\b", re.I),
)
_LOW_CONTEXT_PATTERNS = (
    re.compile(r"(?:论文|作业|新闻|电影|小说|概念|词语).{0,24}(自杀|轻生|想死|self[- ]harm|suicide)", re.I),
    re.compile(r"(自杀|轻生|想死|self[- ]harm|suicide).{0,18}(是什么意思|定义|研究|预防)", re.I),
)
_THIRD_PARTY_PATTERNS = (
    re.compile(r"(?:如何帮助|怎么劝).{0,20}(自杀|轻生|想死|self[- ]harm|suicide)", re.I),
    re.compile(r"(?:朋友|同学|室友|他|她).{0,12}(不想活|想死|自杀|轻生|伤害自己)"),
    re.compile(r"\b(?:friend|roommate|classmate).{0,24}(?:suicide|suicidal|self[- ]harm|want(?:s|ed)? to die)\b", re.I),
)
_CONTRAST = re.compile(r"(?:但是|但|可是|不过|现在却|今晚却|yet|but)", re.I)
_MEDIUM_PATTERNS = (
    re.compile(r"(活着没意思|消失就好了|撑不下去|没有希望|绝望|告别所有人|和所有人告别|交代后事|不值得活)"),
    re.compile(r"\b(?:hopeless|can't go on|cannot go on|better off dead|say goodbye forever)\b", re.I),
)


def detect_risk_signal(text: str) -> RiskRuleResult:
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return RiskRuleResult(None, "未检测到规则风险信号")

    denial = next((pattern.search(normalized) for pattern in _DENIAL_PATTERNS if pattern.search(normalized)), None)
    low_context = next(
        (pattern.search(normalized) for pattern in _LOW_CONTEXT_PATTERNS if pattern.search(normalized)),
        None,
    )
    third_party = next(
        (pattern.search(normalized) for pattern in _THIRD_PARTY_PATTERNS if pattern.search(normalized)),
        None,
    )
    has_contrast_after_denial = bool(denial and _CONTRAST.search(normalized, denial.end()))
    if denial and not has_contrast_after_denial:
        return RiskRuleResult(None, "检测到明确否认表达，交由模型结合上下文复核", denial.group(0))
    if low_context:
        return RiskRuleResult(None, "检测到研究、新闻或概念语境，交由模型结合上下文复核", low_context.group(0))
    if third_party:
        return RiskRuleResult(
            RiskLevel.MEDIUM,
            "检测到第三方或信息性风险话题，需要支持性追问但不触发本人高风险硬判定",
            third_party.group(0),
        )
    for pattern in _HIGH_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return RiskRuleResult(RiskLevel.HIGH, "检测到本人伤害意图或行动计划硬信号", match.group(0))
    for pattern in _MEDIUM_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return RiskRuleResult(RiskLevel.MEDIUM, "检测到绝望、告别或持续无望表达", match.group(0))
    return RiskRuleResult(None, "未检测到规则风险信号")
