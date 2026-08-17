from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.graph.errors import AgentFailureKind, classify_agent_error
from app.core.config import Settings
from app.graph.checkpoint import create_checkpointer
from app.graph.nodes import (
    GraphRuntimeContext,
    assess_safety,
    compact_context,
    finalize,
    gather_context,
    generate_response,
    prefetch_memory,
    review_response,
    route_context_node,
    safe_fallback_node,
    skip_context,
    understand_intent,
)
from app.graph.routing import route_context, route_generation, route_review
from app.graph.state import AgentState


def build_agent_graph(settings: Settings):
    builder = StateGraph(AgentState, context_schema=GraphRuntimeContext)
    retry_policy = RetryPolicy(
        initial_interval=max(0.0, settings.langgraph_retry_base_seconds),
        backoff_factor=2.0,
        max_interval=max(settings.langgraph_retry_base_seconds, settings.langgraph_retry_max_seconds),
        max_attempts=max(1, settings.langgraph_node_max_attempts),
        jitter=settings.langgraph_retry_jitter_ratio > 0,
        retry_on=_retryable_node_error,
    )
    timeout = max(0.2, settings.langgraph_node_timeout_seconds + 0.5)

    builder.add_node("prefetch_memory", prefetch_memory, retry_policy=retry_policy, timeout=timeout)
    builder.add_node("understand_intent", understand_intent, retry_policy=retry_policy, timeout=timeout)
    builder.add_node("assess_safety", assess_safety, retry_policy=retry_policy, timeout=timeout)
    builder.add_node("route_context", route_context_node)
    builder.add_node("gather_context", gather_context, retry_policy=retry_policy, timeout=timeout)
    builder.add_node("skip_context", skip_context)
    builder.add_node("generate_response", generate_response, retry_policy=retry_policy, timeout=timeout)
    builder.add_node("review_response", review_response, retry_policy=retry_policy, timeout=timeout)
    builder.add_node("compact_context", compact_context)
    builder.add_node("safe_fallback", safe_fallback_node)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "prefetch_memory")
    builder.add_edge("prefetch_memory", "understand_intent")
    builder.add_edge("prefetch_memory", "assess_safety")
    builder.add_edge(["understand_intent", "assess_safety"], "route_context")
    builder.add_conditional_edges(
        "route_context",
        route_context,
        {"gather_context": "gather_context", "skip_context": "skip_context"},
    )
    builder.add_edge("gather_context", "generate_response")
    builder.add_edge("skip_context", "generate_response")
    builder.add_conditional_edges(
        "generate_response",
        route_generation,
        {
            "review_response": "review_response",
            "compact_context": "compact_context",
            "safe_fallback": "safe_fallback",
        },
    )
    builder.add_edge("compact_context", "generate_response")
    builder.add_conditional_edges(
        "review_response",
        route_review,
        {
            "generate_response": "generate_response",
            "safe_fallback": "safe_fallback",
            "finalize": "finalize",
        },
    )
    builder.add_edge("safe_fallback", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile(
        checkpointer=create_checkpointer(settings),
        name="mindbridge-agent-runtime",
    )


def _retryable_node_error(exc: Exception) -> bool:
    failure = classify_agent_error(exc)
    return failure.retryable and failure.kind != AgentFailureKind.CONTEXT_OVERFLOW
