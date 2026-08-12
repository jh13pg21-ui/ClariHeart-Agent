"""服务层迁移期间复用注册 Prompt 的轻量入口。"""

from __future__ import annotations

import json
from typing import Any

from app.core.enums import RiskLevel
from app.prompts.registry import PromptRegistry, default_prompt_registry
from app.schemas.dtos import AiMessage


STATIC_GLOBAL_IDS = (
    "global.identity",
    "global.safety_boundary",
    "global.privacy_boundary",
    "global.untrusted_context",
)


def render_registered_prefix(
    agent_prompt_id: str,
    task_prompt_id: str,
    *,
    mode: str = "default",
    locale: str = "zh-CN",
    registry: PromptRegistry | None = None,
) -> str:
    prompt_registry = registry or default_prompt_registry()
    rendered = []
    for prompt_id in (*STATIC_GLOBAL_IDS, agent_prompt_id, task_prompt_id):
        definition = prompt_registry.get(prompt_id)
        available = {"mode": mode, "locale": locale}
        variables = {name: available[name] for name in definition.variables}
        item = prompt_registry.render(prompt_id, variables)
        rendered.append(
            f"[TRUSTED_INSTRUCTION id={item.prompt_id} version={item.version}]\n{item.content}"
        )
    return "\n\n".join(rendered)


def registered_task_messages(
    agent_prompt_id: str,
    task_prompt_id: str,
    payload: Any,
    *,
    mode: str = "default",
    locale: str = "zh-CN",
) -> list[AiMessage]:
    return [
        AiMessage(
            role="system",
            content=render_registered_prefix(
                agent_prompt_id,
                task_prompt_id,
                mode=mode,
                locale=locale,
            ),
        ),
        AiMessage(
            role="user",
            content=json.dumps(
                {"trust": "UNTRUSTED_DATA", "payload": payload},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    ]


async def registered_complete(
    ai,
    *,
    agent_name: str,
    agent_prompt_id: str,
    task_name: str,
    payload: Any,
    risk_level: RiskLevel = RiskLevel.LOW,
    mode: str = "default",
) -> str:
    if hasattr(ai, "complete_registered_task"):
        result = await ai.complete_registered_task(
            agent_name=agent_name,
            agent_prompt_id=agent_prompt_id,
            task_name=task_name,
            payload=payload,
            risk_level=risk_level,
            mode=mode,
        )
        return result.text
    return await ai.complete(
        registered_task_messages(
            agent_prompt_id,
            f"task.{task_name}",
            payload,
            mode=mode,
        )
    )
