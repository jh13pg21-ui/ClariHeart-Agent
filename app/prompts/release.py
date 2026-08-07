"""Prompt 与输出 Schema 的不可变发布清单。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.prompts.registry import PromptRegistry, PromptReleaseEntry, default_prompt_registry


PROMPT_RELEASE = "2026.08-v2"


@dataclass(frozen=True)
class PromptReleaseManifest:
    release: str
    tasks: tuple[str, ...]
    prompts: dict[str, PromptReleaseEntry]
    schema_hashes: dict[str, str]
    manifest_hash: str


def default_prompt_release(
    registry: PromptRegistry | None = None,
) -> PromptReleaseManifest:
    prompt_registry = registry or default_prompt_registry()
    prompts = prompt_registry.release_manifest()
    tasks = tuple(
        prompt_id.removeprefix("task.")
        for prompt_id in prompts
        if prompt_id.startswith("task.")
    )
    schema_hashes = prompt_registry.schema_hashes()
    canonical = json.dumps(
        {
            "release": PROMPT_RELEASE,
            "prompts": {
                prompt_id: {
                    "version": entry.version,
                    "hash": entry.content_hash,
                }
                for prompt_id, entry in sorted(prompts.items())
            },
            "schemas": dict(sorted(schema_hashes.items())),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return PromptReleaseManifest(
        release=PROMPT_RELEASE,
        tasks=tasks,
        prompts=prompts,
        schema_hashes=schema_hashes,
        manifest_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )
