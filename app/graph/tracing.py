from __future__ import annotations

import os
from typing import Any

from app.core.config import Settings


def configure_langsmith(settings: Settings) -> None:
    enabled = bool(settings.langsmith_tracing_enabled)
    os.environ["LANGSMITH_TRACING"] = "true" if enabled else "false"
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if not settings.langsmith_allow_content:
        os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"
    else:
        os.environ.pop("LANGSMITH_HIDE_INPUTS", None)
        os.environ.pop("LANGSMITH_HIDE_OUTPUTS", None)


def graph_config(turn_id: str, runtime_name: str) -> dict[str, Any]:
    # 只允许低敏、低基数字段进入外部 Trace metadata。
    return {
        "configurable": {"thread_id": turn_id},
        "metadata": {
            "turn_id": turn_id,
            "runtime": runtime_name,
        },
        "tags": ["mindbridge", runtime_name],
    }
