"""稳定前缀、隔离动态附件并生成无正文 Prompt Manifest。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.context.contracts import ContextPlan, ContextSection
from app.context.tokens import TokenEstimator
from app.prompts.registry import PromptRegistry
from app.prompts.release import PROMPT_RELEASE
from app.schemas.dtos import AiMessage


@dataclass(frozen=True)
class PromptRequest:
    request_id: str
    agent_name: str
    task_name: str
    agent_prompt_id: str
    task_prompt_id: str
    mode: str = "default"
    locale: str = "zh-CN"


@dataclass(frozen=True)
class PromptManifestSection:
    section_id: str
    version: str
    content_hash: str
    token_count: int
    loading_reason: str
    trust: str
    category: str
    provenance_hash: str


@dataclass(frozen=True)
class PromptManifest:
    release: str
    request_id: str
    static_prefix_hash: str
    dynamic_context_hash: str
    manifest_hash: str
    context_plan_hash: str
    total_tokens: int
    sections: tuple[PromptManifestSection, ...]


@dataclass(frozen=True)
class AssembledPrompt:
    messages: tuple[AiMessage, ...]
    manifest: PromptManifest
    context_section_ids: tuple[str, ...]


class PromptAssembler:
    STATIC_GLOBAL_IDS = (
        "global.identity",
        "global.safety_boundary",
        "global.privacy_boundary",
        "global.untrusted_context",
    )

    def __init__(self, registry: PromptRegistry, estimator: TokenEstimator) -> None:
        self.registry = registry
        self.estimator = estimator

    def assemble(
        self,
        request: PromptRequest,
        context_plan: ContextPlan,
    ) -> AssembledPrompt:
        prompt_ids = self.STATIC_GLOBAL_IDS + (
            request.agent_prompt_id,
            request.task_prompt_id,
        )
        rendered = [self._render(prompt_id, request) for prompt_id in prompt_ids]
        static_content = "\n\n".join(
            f"[TRUSTED_INSTRUCTION id={item.prompt_id} version={item.version}]\n{item.content}"
            for item in rendered
        )
        messages: list[AiMessage] = [AiMessage(role="system", content=static_content)]

        attachments: list[ContextSection] = []
        history: list[ContextSection] = []
        current: ContextSection | None = None
        for section in context_plan.sections:
            if section.id == "user.current":
                current = section
            elif section.category == "conversation" and section.message_role:
                history.append(section)
            else:
                attachments.append(section)

        for section in attachments:
            payload = json.dumps(
                {
                    "type": "CONTEXT_ATTACHMENT",
                    "trust": "UNTRUSTED_DATA",
                    "sectionId": section.id,
                    "category": section.category,
                    "sourceIds": list(section.provenance_ids),
                    "content": section.content,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            messages.append(AiMessage(role="system", content="UNTRUSTED_DATA\n" + payload))
        for section in history:
            messages.append(AiMessage(role=section.message_role, content=section.content))
        if current is not None:
            messages.append(AiMessage(role="user", content=current.content))

        dynamic_payload = json.dumps(
            [
                {
                    "id": section.id,
                    "hash": section.content_hash,
                    "role": section.message_role,
                    "provenance": list(section.provenance_ids),
                }
                for section in context_plan.sections
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_sections = tuple(
            [
                PromptManifestSection(
                    section_id=item.prompt_id,
                    version=item.version,
                    content_hash=item.content_hash,
                    token_count=self.estimator.count(item.content),
                    loading_reason="prompt_registry",
                    trust="TRUSTED_INSTRUCTION",
                    category="prompt",
                    provenance_hash="",
                )
                for item in rendered
            ]
            + [self._context_manifest_section(section) for section in context_plan.sections]
        )
        static_hash = _sha256(static_content)
        dynamic_hash = _sha256(dynamic_payload)
        total_tokens = self.estimator.count_messages(messages)
        manifest_hash = self._manifest_hash(
            request,
            static_hash,
            dynamic_hash,
            context_plan.plan_hash,
            total_tokens,
            manifest_sections,
        )
        manifest = PromptManifest(
            release=PROMPT_RELEASE,
            request_id=request.request_id,
            static_prefix_hash=static_hash,
            dynamic_context_hash=dynamic_hash,
            manifest_hash=manifest_hash,
            context_plan_hash=context_plan.plan_hash,
            total_tokens=total_tokens,
            sections=manifest_sections,
        )
        return AssembledPrompt(
            messages=tuple(messages),
            manifest=manifest,
            context_section_ids=tuple(section.id for section in context_plan.sections),
        )

    def _render(self, prompt_id: str, request: PromptRequest):
        definition = self.registry.get(prompt_id)
        available = {"mode": request.mode, "locale": request.locale}
        variables = {name: available[name] for name in definition.variables}
        return self.registry.render(prompt_id, variables)

    @staticmethod
    def _context_manifest_section(section: ContextSection) -> PromptManifestSection:
        provenance = json.dumps(
            list(section.provenance_ids),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return PromptManifestSection(
            section_id=section.id,
            version="context-v1",
            content_hash=section.content_hash,
            token_count=section.token_count,
            loading_reason=section.loading_reason,
            trust=section.trust,
            category=section.category,
            provenance_hash=_sha256(provenance) if section.provenance_ids else "",
        )

    @staticmethod
    def _manifest_hash(
        request: PromptRequest,
        static_hash: str,
        dynamic_hash: str,
        plan_hash: str,
        total_tokens: int,
        sections: tuple[PromptManifestSection, ...],
    ) -> str:
        canonical = json.dumps(
            {
                "release": PROMPT_RELEASE,
                "requestId": request.request_id,
                "agent": request.agent_name,
                "task": request.task_name,
                "static": static_hash,
                "dynamic": dynamic_hash,
                "plan": plan_hash,
                "tokens": total_tokens,
                "sections": [
                    {
                        "id": section.section_id,
                        "version": section.version,
                        "hash": section.content_hash,
                        "tokens": section.token_count,
                        "reason": section.loading_reason,
                        "trust": section.trust,
                        "category": section.category,
                        "provenance": section.provenance_hash,
                    }
                    for section in sections
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _sha256(canonical)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
