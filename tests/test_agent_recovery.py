import unittest

from app.agents.events import AgentArtifact, CollaborationBlackboard
from app.agents.recovery import (
    AgentFailureKind,
    classify_agent_error,
    compact_board_for_retry,
)
from app.llm.errors import ModelError, ModelErrorCode


def _model_error(code: ModelErrorCode, *, retryable: bool = True) -> ModelError:
    return ModelError(
        code=code,
        message=code.value,
        retryable=retryable,
        provider="ollama",
        model="local-model",
    )


class AgentRecoveryTests(unittest.TestCase):
    def test_typed_prompt_too_long_is_context_overflow(self):
        failure = classify_agent_error(_model_error(ModelErrorCode.PROMPT_TOO_LONG))

        self.assertEqual(failure.kind, AgentFailureKind.CONTEXT_OVERFLOW)
        self.assertTrue(failure.retryable)
        self.assertEqual(failure.error_type, "ModelError")

    def test_typed_permanent_model_error_does_not_inherit_string_markers(self):
        failure = classify_agent_error(
            ModelError(
                code=ModelErrorCode.CONTENT_POLICY,
                message="prompt too long 不是本次失败原因",
                retryable=False,
                provider="ollama",
                model="local-model",
            )
        )

        self.assertEqual(failure.kind, AgentFailureKind.PERMANENT)
        self.assertFalse(failure.retryable)

    def test_retry_board_only_marks_reactive_plan_without_mutating_evidence(self):
        payload = {
            "modelHistory": [f"message-{index}" for index in range(10)],
            "retrievedKnowledge": ["evidence-a", "evidence-b", "evidence-c"],
            "skillContext": "skill" * 1000,
            "longTermMemoryContext": "memory" * 1000,
        }
        board = CollaborationBlackboard(turn_id="turn").add_artifact(
            AgentArtifact(
                id="context",
                owner="ContextAgent",
                kind="context",
                payload=payload,
            )
        )

        retry_board = compact_board_for_retry(board)
        retry_artifact = retry_board.latest_artifact("context")

        self.assertEqual(retry_artifact.payload, payload)
        self.assertEqual(board.latest_artifact("context").metadata, {})
        self.assertTrue(retry_artifact.metadata["reactiveCompacted"])
        self.assertEqual(retry_artifact.metadata["contextRecoveryAttempt"], 1)
