"""带来源 allowlist 的局部 LLM 压缩与确定性降级。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace

from app.context.contracts import ContextSection
from app.context.tokens import TokenEstimator
from app.core.enums import RiskLevel
from app.llm.contracts import ModelRequest
from app.llm.errors import ModelError
from app.llm.gateway import ModelGateway
from app.prompts.registry import PromptRegistry, default_prompt_registry
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer


class SectionCompactionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SectionCompactor:
    def __init__(
        self,
        gateway: ModelGateway,
        estimator: TokenEstimator,
        *,
        local_provider: str,
        local_model: str,
        registry: PromptRegistry | None = None,
    ) -> None:
        self.gateway = gateway
        self.estimator = estimator
        self.local_provider = local_provider
        self.local_model = local_model
        self.registry = registry or default_prompt_registry()
        self.sanitizer = PrivacySanitizer()

    async def compact(
        self,
        sections: tuple[ContextSection, ...] | list[ContextSection],
        target_tokens: int,
        risk_level: RiskLevel,
    ) -> tuple[ContextSection, ...]:
        source = tuple(sections)
        if target_tokens <= 0:
            raise SectionCompactionError("INVALID_TARGET", "target_tokens 必须大于 0")
        if sum(self._tokens(section) for section in source) <= target_tokens:
            return source
        required = tuple(section for section in source if section.required)
        required_tokens = sum(self._tokens(section) for section in required)
        if required_tokens > target_tokens:
            raise SectionCompactionError(
                "REQUIRED_CONTEXT_TOO_LARGE",
                "必需 section 超过局部压缩目标，不能修改",
            )

        groups: dict[str, list[ContextSection]] = {}
        for section in source:
            if not section.required:
                groups.setdefault(section.category, []).append(section)
        available = target_tokens - required_tokens
        per_group_target = max(8, available // max(1, len(groups)))
        compacted_groups: dict[str, ContextSection] = {}
        for category, group in groups.items():
            try:
                compacted = await self._llm_compact_group(
                    tuple(group),
                    per_group_target,
                    risk_level,
                )
            except (ModelError, ValueError):
                compacted = self._deterministic_group(tuple(group), per_group_target)
            compacted_groups[category] = compacted

        result: list[ContextSection] = []
        emitted_categories = set()
        for section in source:
            if section.required:
                result.append(section)
                continue
            if section.category not in emitted_categories:
                result.append(compacted_groups[section.category])
                emitted_categories.add(section.category)
        return self._fit_to_target(tuple(result), target_tokens)

    async def _llm_compact_group(
        self,
        sections: tuple[ContextSection, ...],
        target_tokens: int,
        risk_level: RiskLevel,
    ) -> ContextSection:
        allowed_sources = tuple(
            dict.fromkeys(
                source_id
                for section in sections
                for source_id in (section.provenance_ids or (section.id,))
            )
        )
        identity = self.registry.render("global.identity").content
        boundary = self.registry.render("global.untrusted_context").content
        task = self.registry.render("task.context_section_summary").content
        attachment = {
            "targetTokens": target_tokens,
            "sections": [
                {
                    "sectionId": section.id,
                    "sourceIds": list(section.provenance_ids or (section.id,)),
                    "content": self.sanitizer.sanitize(section.content),
                }
                for section in sections
            ],
        }
        messages = (
            AiMessage(
                role="system",
                content=f"{identity}\n\n{boundary}\n\n{task}",
            ),
            AiMessage(
                role="user",
                content=json.dumps(
                    attachment,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        cloud_allowed = risk_level is not RiskLevel.HIGH
        request = ModelRequest(
            request_id=uuid.uuid4().hex,
            agent_name="ContextSectionCompactor",
            task_name="context_section_summary",
            risk_level=risk_level,
            messages=messages,
            preferred_provider=self.local_provider,
            preferred_model=self.local_model,
            max_output_tokens=max(64, target_tokens),
            output_schema_id="context_section_summary_v1",
            cloud_egress_allowed=cloud_allowed,
            sanitized=cloud_allowed,
            context_section_ids=tuple(section.id for section in sections),
        )
        result = await self.gateway.complete(request)
        payload = json.loads(_strip_code_fence(result.text))
        if not isinstance(payload, dict):
            raise ValueError("压缩输出顶层不是对象")
        try:
            summary = self.sanitizer.sanitize(str(payload["summary"])).strip()
            raw_source_ids = payload["sourceIds"]
        except KeyError as exc:
            raise ValueError("压缩输出缺少 summary/sourceIds") from exc
        if not summary or not isinstance(raw_source_ids, list):
            raise ValueError("压缩输出缺少 summary/sourceIds")
        valid_sources = tuple(
            dict.fromkeys(
                str(source_id)
                for source_id in raw_source_ids
                if str(source_id) in allowed_sources
            )
        )
        if not valid_sources:
            raise ValueError("压缩输出没有有效来源")
        return self._summary_section(sections, summary, valid_sources, "llm_section_compaction")

    def _deterministic_group(
        self,
        sections: tuple[ContextSection, ...],
        target_tokens: int,
    ) -> ContextSection:
        provenance = tuple(
            dict.fromkeys(
                source_id
                for section in sections
                for source_id in (section.provenance_ids or (section.id,))
            )
        )
        joined = "\n".join(self.sanitizer.sanitize(section.content) for section in sections)
        content = self._trim_text(joined, max(1, target_tokens - 6))
        return self._summary_section(
            sections,
            content,
            provenance,
            "deterministic_head_tail_compaction",
        )

    def _fit_to_target(
        self,
        sections: tuple[ContextSection, ...],
        target_tokens: int,
    ) -> tuple[ContextSection, ...]:
        required = [section for section in sections if section.required]
        optional = [section for section in sections if not section.required]
        required_tokens = sum(self._tokens(section) for section in required)
        available = target_tokens - required_tokens
        if not optional:
            return tuple(required)
        per_section = max(7, available // len(optional))
        fitted = []
        for section in optional:
            if self._tokens(section) <= per_section:
                fitted.append(section)
                continue
            content = self._trim_text(section.content, max(1, per_section - 6))
            fitted.append(
                replace(
                    section,
                    content=content,
                    token_count=self.estimator.count(content) + 6,
                    loading_reason=section.loading_reason + ":fitted",
                )
            )
        result = tuple(required + fitted)
        if sum(self._tokens(section) for section in result) > target_tokens:
            raise SectionCompactionError(
                "TARGET_UNREACHABLE",
                "保留分类与来源后无法达到局部压缩目标",
            )
        return result

    def _summary_section(
        self,
        sections: tuple[ContextSection, ...],
        content: str,
        provenance_ids: tuple[str, ...],
        reason: str,
    ) -> ContextSection:
        digest = hashlib.sha256(
            "|".join(section.id for section in sections).encode("utf-8")
        ).hexdigest()[:12]
        sensitivity = max(
            (section.sensitivity for section in sections),
            key=lambda risk: {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}[risk],
        )
        return ContextSection(
            id=f"compact.{sections[0].category}.{digest}",
            category=sections[0].category,
            content=content,
            priority=max(section.priority for section in sections),
            required=False,
            trust="UNTRUSTED_DATA",
            sensitivity=sensitivity,
            provenance_ids=provenance_ids,
            loading_reason=reason,
            token_count=self.estimator.count(content) + 6,
            compaction_level=max(section.compaction_level for section in sections) + 1,
        )

    def _trim_text(self, text: str, token_budget: int) -> str:
        value = str(text or "").strip()
        if self.estimator.count(value) <= token_budget:
            return value
        low, high = 1, len(value)
        best = ""
        while low <= high:
            size = (low + high) // 2
            head_size = max(1, size * 2 // 3)
            tail_size = max(0, size - head_size)
            candidate = value[:head_size]
            if tail_size:
                candidate += "…" + value[-tail_size:]
            if self.estimator.count(candidate) <= token_budget:
                best = candidate
                low = size + 1
            else:
                high = size - 1
        return best or value[:1]

    def _tokens(self, section: ContextSection) -> int:
        return section.token_count or self.estimator.count(section.content) + 6


def _strip_code_fence(text: str) -> str:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s*```$", "", value).strip()
