from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.enums import IntentType, RiskLevel
from app.models.entities import PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage


class SkillLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillValidationIssue:
    level: str
    message: str


@dataclass(frozen=True)
class MindBridgeSkill:
    name: str
    description: str
    body: str
    path: Path
    metadata: dict[str, str] = field(default_factory=dict)

    def prompt_context(self) -> str:
        return f"应用 skill: {self.name}\n{self.body.strip()}"

    def validation_issues(self) -> list[SkillValidationIssue]:
        issues: list[SkillValidationIssue] = []
        if self.path.parent.name != self.name:
            issues.append(SkillValidationIssue("WARN", f"目录名 {self.path.parent.name} 与 skill name {self.name} 不一致"))
        if "## Workflow" not in self.body:
            issues.append(SkillValidationIssue("WARN", "建议包含 ## Workflow 小节，便于人工审阅和模型稳定加载"))
        if len(self.description) < 20:
            issues.append(SkillValidationIssue("WARN", "description 太短，可能无法准确表达触发场景"))
        if self.name == "counselor_handoff_summary" and "```text" not in self.body:
            issues.append(SkillValidationIssue("ERROR", "counselor_handoff_summary 必须包含 text 模板"))
        return issues


@dataclass(frozen=True)
class SkillSelection:
    names: tuple[str, ...]
    strategy: str
    semantic_error: str = ""


class MindBridgeSkillRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[2] / "skills"

    def list_skills(self) -> list[MindBridgeSkill]:
        if not self.root.exists():
            return []
        skills = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            skills.append(self._load_skill_file(skill_file))
        return skills

    def status_items(self) -> list[dict]:
        if not self.root.exists():
            return []
        items = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            try:
                skill = self._load_skill_file(skill_file)
                issues = skill.validation_issues()
            except SkillLoadError as exc:
                items.append(
                    {
                        "name": skill_file.parent.name,
                        "status": "FAILED",
                        "description": str(exc),
                        "path": skill_file.relative_to(self.root.parent).as_posix(),
                        "issues": [{"level": "ERROR", "message": str(exc)}],
                    }
                )
                continue
            has_error = any(issue.level == "ERROR" for issue in issues)
            items.append(
                {
                    "name": skill.name,
                    "status": "FAILED" if has_error else "READY" if not issues else "WARN",
                    "description": skill.description,
                    "path": skill.path.relative_to(self.root.parent).as_posix(),
                    "issues": [{"level": issue.level, "message": issue.message} for issue in issues],
                    "metadata": skill.metadata,
                }
            )
        return items

    def get_required(self, name: str) -> MindBridgeSkill:
        for skill in self.list_skills():
            if skill.name == name:
                return skill
        raise SkillLoadError(f"required standard skill not found: {name}")

    def template_for(self, name: str) -> str:
        skill = self.get_required(name)
        match = re.search(r"```text\s*\n(?P<template>.*?)\n```", skill.body, re.DOTALL)
        if match is None:
            raise SkillLoadError(f"standard skill {name} does not define a text template")
        return match.group("template").strip()

    def _load_skill_file(self, path: Path) -> MindBridgeSkill:
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text, path)
        name = metadata.get("name") or path.parent.name
        description = metadata.get("description", "")
        if not name.strip():
            raise SkillLoadError(f"{path} is missing frontmatter name")
        if not description.strip():
            raise SkillLoadError(f"{path} is missing frontmatter description")
        if not body.strip():
            raise SkillLoadError(f"{path} is missing skill body")
        return MindBridgeSkill(name=name.strip(), description=description.strip(), body=body.strip(), path=path, metadata=metadata)


class MindBridgeSkillLibrary:
    _MANDATORY_SUPPORT_SKILLS = (
        "supportive_response_baseline",
        "referral_resource_guidance",
    )
    _HIGH_RISK_SKILLS = (
        "supportive_response_baseline",
        "high_risk_safety_plan",
    )
    _OPTIONAL_RESPONSE_SKILLS = (
        "anxiety_grounding_support",
        "sleep_routine_support",
        "academic_stress_planning",
    )

    @staticmethod
    def registry() -> MindBridgeSkillRegistry:
        return MindBridgeSkillRegistry()

    @staticmethod
    def list_skills() -> list[MindBridgeSkill]:
        return MindBridgeSkillLibrary.registry().list_skills()

    @staticmethod
    def status_items() -> list[dict]:
        return MindBridgeSkillLibrary.registry().status_items()

    @staticmethod
    def response_skill_context(intent: IntentType, risk: RiskLevel, text: str) -> str:
        names = MindBridgeSkillLibrary.response_skill_names(intent, risk, text)
        registry = MindBridgeSkillLibrary.registry()
        return "\n\n".join(registry.get_required(name).prompt_context() for name in names)

    @staticmethod
    async def select_response_skills(
        intent: IntentType,
        risk: RiskLevel,
        text: str,
        ai_client,
        *,
        semantic_enabled: bool = True,
        max_optional: int = 2,
    ) -> SkillSelection:
        """选择学生端回复 Skill。

        风险策略永远先于模型选择：普通聊天不加载，高风险固定加载安全计划；
        只有普通心理支持场景允许模型从受控白名单中补充可选 Skill。
        """
        fallback_names = MindBridgeSkillLibrary.response_skill_names(intent, risk, text)
        if intent == IntentType.CHAT and risk == RiskLevel.LOW:
            return SkillSelection(tuple(fallback_names), "chat_skip")
        if risk == RiskLevel.HIGH:
            return SkillSelection(tuple(fallback_names), "high_risk_hard_guard")
        if not semantic_enabled or ai_client is None:
            return SkillSelection(tuple(fallback_names), "rule_only")

        fallback_optional = [
            name
            for name in fallback_names
            if name in MindBridgeSkillLibrary._OPTIONAL_RESPONSE_SKILLS
        ][:max(0, max_optional)]
        try:
            semantic_names = await MindBridgeSkillLibrary._semantic_optional_skills(
                text,
                ai_client,
                max_optional=max_optional,
            )
        except Exception as exc:
            return SkillSelection(
                tuple(fallback_names),
                "rule_fallback",
                f"{type(exc).__name__}: {exc}",
            )

        mandatory = list(MindBridgeSkillLibrary._MANDATORY_SUPPORT_SKILLS)
        if semantic_names:
            return SkillSelection(
                tuple(_dedupe([*mandatory, *semantic_names])),
                "semantic_ranked",
            )
        return SkillSelection(
            tuple(_dedupe([*mandatory, *fallback_optional])),
            "rule_fallback_empty",
        )

    @staticmethod
    async def response_skill_context_async(
        intent: IntentType,
        risk: RiskLevel,
        text: str,
        ai_client,
        *,
        semantic_enabled: bool = True,
        max_optional: int = 2,
    ) -> tuple[str, SkillSelection]:
        selection = await MindBridgeSkillLibrary.select_response_skills(
            intent,
            risk,
            text,
            ai_client,
            semantic_enabled=semantic_enabled,
            max_optional=max_optional,
        )
        registry = MindBridgeSkillLibrary.registry()
        context = "\n\n".join(
            registry.get_required(name).prompt_context() for name in selection.names
        )
        return context, selection

    @staticmethod
    def response_skill_names(intent: IntentType, risk: RiskLevel, text: str) -> list[str]:
        if risk == RiskLevel.HIGH:
            return list(MindBridgeSkillLibrary._HIGH_RISK_SKILLS)

        if intent == IntentType.CHAT and risk == RiskLevel.LOW:
            return []

        lowered = text.lower()
        names = list(MindBridgeSkillLibrary._MANDATORY_SUPPORT_SKILLS)
        if _contains_any(lowered, ["焦虑", "惊恐", "恐慌", "panic", "anxious", "崩溃", "呼吸"]):
            names.append("anxiety_grounding_support")
        if _contains_any(lowered, ["失眠", "睡不着", "睡眠", "熬夜", "sleep", "insomnia"]):
            names.append("sleep_routine_support")
        if _contains_any(lowered, ["考试", "挂科", "绩点", "论文", "作业", "学业", "学习", "academic", "exam"]):
            names.append("academic_stress_planning")
        return _dedupe(names)

    @staticmethod
    async def _semantic_optional_skills(text: str, ai_client, *, max_optional: int) -> list[str]:
        max_optional = max(0, min(max_optional, len(MindBridgeSkillLibrary._OPTIONAL_RESPONSE_SKILLS)))
        if max_optional == 0:
            return []
        registry = MindBridgeSkillLibrary.registry()
        candidates = [
            registry.get_required(name)
            for name in MindBridgeSkillLibrary._OPTIONAL_RESPONSE_SKILLS
        ]
        catalog = "\n".join(
            f"- {skill.name}: {skill.description}" for skill in candidates
        )
        raw = await ai_client.complete(
            [
                AiMessage(
                    role="system",
                    content=(
                        "你是 MindBridge 的 Skill 选择器。只从候选列表选择最相关的学生支持 Skill，"
                        "不要选择高风险安全计划，不要解释。严格输出 JSON："
                        '{"skills":["skill_name"]}。最多选择 '
                        f"{max_optional} 个。候选列表：\n{catalog}"
                    ),
                ),
                AiMessage(role="user", content=text),
            ]
        )
        data = json.loads(_extract_json_object(raw))
        values = data.get("skills", [])
        if not isinstance(values, list):
            raise ValueError("semantic skill selector returned non-list skills")
        allowed = set(MindBridgeSkillLibrary._OPTIONAL_RESPONSE_SKILLS)
        selected = [str(name) for name in values if str(name) in allowed]
        return _dedupe(selected)[:max_optional]

    @staticmethod
    def high_risk_safety_plan_prompt() -> str:
        return MindBridgeSkillLibrary.registry().get_required("high_risk_safety_plan").prompt_context()

    @staticmethod
    def counselor_handoff_summary(report: PsychologicalReport, user: UserAccount | None) -> str:
        template = MindBridgeSkillLibrary.registry().template_for("counselor_handoff_summary")
        student = _student_label(user, report.user_id)
        urgency = "立即跟进" if report.risk_level == RiskLevel.HIGH.value else "尽快跟进"
        next_steps = [
            f"{urgency}，确认学生当前位置、身边是否有人陪伴，以及当前是否安全。",
            "联系学生本人或其可用的现实支持人，并记录已采取的联系方式。",
            "必要时联系校园保卫、心理中心值班老师或当地紧急救助。",
            "将后续安排、接手人和下一次复访时间写入个案备注。",
        ]
        return _render_template(
            template,
            {
                "report_id": str(report.id),
                "student": student,
                "risk_level": report.risk_level,
                "emotion": report.emotion,
                "confidence": f"{report.confidence:.2f}",
                "summary": report.summary,
                "next_steps": "\n".join(f"- {step}" for step in next_steps),
                "content_excerpt": _truncate(report.content, 700),
            },
        )


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillLoadError(f"{path} is missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise SkillLoadError(f"{path} has unterminated YAML frontmatter")
    metadata = {}
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise SkillLoadError(f"{path} has invalid frontmatter line: {line}")
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[end + len("\n---") :].strip()


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _extract_json_object(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("semantic skill selector did not return JSON")
    return raw[start : end + 1]


def _render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _student_label(user: UserAccount | None, user_id: int) -> str:
    if user is None:
        return f"userId={user_id}"
    if user.display_name:
        return f"{user.display_name} ({user.username})"
    return user.username


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 3]}..."
