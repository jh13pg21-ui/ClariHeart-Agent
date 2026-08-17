from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.channels import UntrackedValue


class AgentState(TypedDict, total=False):
    turn_id: str
    user_id: int
    session_id: str
    model_input: str

    # 下列字段为节点工作集，包含会话/检索/候选正文，禁止写入 checkpoint。
    memory: Annotated[dict[str, Any], UntrackedValue(dict)]
    intent: dict[str, Any]
    risk: dict[str, Any]
    context: Annotated[dict[str, Any], UntrackedValue(dict)]
    response_candidate: Annotated[dict[str, Any], UntrackedValue(dict)]
    output_safety: Annotated[dict[str, Any], UntrackedValue(dict)]

    artifacts: Annotated[list[dict[str, Any]], add]
    domain_events: Annotated[list[dict[str, Any]], add]
    errors: Annotated[list[dict[str, Any]], add]
    steps: Annotated[list[dict[str, Any]], add]

    revision_count: int
    reactive_compaction_used: bool
    generation_route: str
    status: str
    final_response: Annotated[str, UntrackedValue(str)]
    final_artifact_id: str


def initial_agent_state(
    *,
    turn_id: str,
    user_id: int,
    session_id: str,
    model_input: str,
) -> AgentState:
    return {
        "turn_id": turn_id,
        "user_id": user_id,
        "session_id": session_id,
        "model_input": model_input,
        "artifacts": [],
        "domain_events": [],
        "errors": [],
        "steps": [],
        "revision_count": 0,
        "reactive_compaction_used": False,
        "generation_route": "GENERATE",
        "status": "RUNNING",
    }
