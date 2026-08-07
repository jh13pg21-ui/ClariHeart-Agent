"""显式 Prompt Registry；不在运行时扫描目录。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from jinja2 import Environment, StrictUndefined, TemplateError


class PromptRegistryError(RuntimeError):
    pass


class PromptRenderError(PromptRegistryError):
    pass


class PromptVersionMismatch(PromptRegistryError):
    pass


@dataclass(frozen=True)
class PromptDefinition:
    prompt_id: str
    version: str
    relative_path: str
    variables: tuple[str, ...] = ()
    output_schema_id: str = ""


@dataclass(frozen=True)
class RenderedPrompt:
    prompt_id: str
    version: str
    content: str
    content_hash: str
    output_schema_id: str = ""


@dataclass(frozen=True)
class PromptReleaseEntry:
    prompt_id: str
    version: str
    content_hash: str


class PromptRegistry:
    def __init__(
        self,
        root: Path,
        definitions: tuple[PromptDefinition, ...],
    ) -> None:
        self.root = root.resolve()
        self._definitions: dict[str, PromptDefinition] = {}
        self._paths: dict[str, Path] = {}
        self._environment = Environment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        for definition in definitions:
            self._register(definition)

    @property
    def prompt_ids(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def get(self, prompt_id: str) -> PromptDefinition:
        try:
            return self._definitions[prompt_id]
        except KeyError as exc:
            raise PromptRegistryError(f"未注册 Prompt: {prompt_id}") from exc

    def render(
        self,
        prompt_id: str,
        variables: Mapping[str, object] | None = None,
    ) -> RenderedPrompt:
        definition = self.get(prompt_id)
        values = dict(variables or {})
        expected = set(definition.variables)
        received = set(values)
        if received != expected:
            missing = sorted(expected - received)
            unexpected = sorted(received - expected)
            raise PromptRenderError(
                f"Prompt 变量不匹配: missing={missing}, unexpected={unexpected}"
            )
        source = self._read(prompt_id)
        try:
            content = self._environment.from_string(source).render(**values).strip()
        except TemplateError as exc:
            raise PromptRenderError(f"Prompt 渲染失败: {prompt_id}") from exc
        return RenderedPrompt(
            prompt_id=definition.prompt_id,
            version=definition.version,
            content=content,
            content_hash=_sha256(content),
            output_schema_id=definition.output_schema_id,
        )

    def release_manifest(self) -> dict[str, PromptReleaseEntry]:
        return {
            prompt_id: PromptReleaseEntry(
                prompt_id=prompt_id,
                version=definition.version,
                content_hash=_sha256(self._read(prompt_id)),
            )
            for prompt_id, definition in self._definitions.items()
        }

    def verify_release(self, recorded: Mapping[str, PromptReleaseEntry]) -> None:
        current = self.release_manifest()
        for prompt_id, old in recorded.items():
            new = current.get(prompt_id)
            if new is None:
                raise PromptVersionMismatch(f"Prompt 已从发布中移除: {prompt_id}")
            if old.version == new.version and old.content_hash != new.content_hash:
                raise PromptVersionMismatch(
                    f"Prompt 内容已变化但版本未升级: {prompt_id}@{new.version}"
                )

    def _register(self, definition: PromptDefinition) -> None:
        if definition.prompt_id in self._definitions:
            raise PromptRegistryError(f"Prompt ID 重复: {definition.prompt_id}")
        if not re.fullmatch(r"\d+\.\d+\.\d+", definition.version):
            raise PromptRegistryError(
                f"Prompt 版本不是 semver: {definition.prompt_id}@{definition.version}"
            )
        path = (self.root / definition.relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise PromptRegistryError(f"Prompt 路径越界: {definition.relative_path}")
        if len(definition.variables) != len(set(definition.variables)):
            raise PromptRegistryError(f"Prompt 变量重复: {definition.prompt_id}")
        self._definitions[definition.prompt_id] = definition
        self._paths[definition.prompt_id] = path

    def _read(self, prompt_id: str) -> str:
        path = self._paths[prompt_id]
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptRegistryError(f"无法读取 Prompt: {prompt_id} ({path})") from exc


CORE_PROMPT_DEFINITIONS = (
    PromptDefinition("global.identity", "1.0.0", "global/identity.md"),
    PromptDefinition("global.safety_boundary", "1.0.0", "global/safety_boundary.md"),
    PromptDefinition("global.privacy_boundary", "1.0.0", "global/privacy_boundary.md"),
    PromptDefinition("global.untrusted_context", "1.0.0", "global/untrusted_context.md"),
    PromptDefinition("agent.understanding", "1.0.0", "agents/understanding.md"),
    PromptDefinition("agent.safety", "1.0.0", "agents/safety.md"),
    PromptDefinition("agent.context", "1.0.0", "agents/context.md"),
    PromptDefinition(
        "agent.response",
        "1.0.0",
        "agents/response.md",
        variables=("mode", "locale"),
    ),
    PromptDefinition("agent.coordinator", "1.0.0", "agents/coordinator.md"),
)


def default_prompt_registry(root: Path | None = None) -> PromptRegistry:
    prompt_root = root or Path(__file__).resolve().parent
    return PromptRegistry(prompt_root, CORE_PROMPT_DEFINITIONS)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
