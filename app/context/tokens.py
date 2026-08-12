"""Provider-aware token 估算与输入预算计算。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol, Sequence

from app.llm.capabilities import ModelCapabilities
from app.schemas.dtos import AiMessage


class TokenBudgetError(ValueError):
    pass


class TokenEstimator(Protocol):
    def count(self, text: str) -> int: ...

    def count_messages(self, messages: Sequence[AiMessage]) -> int: ...


class _MessageCountingMixin:
    message_overhead = 4
    reply_priming_overhead = 2

    def count_messages(self, messages: Sequence[AiMessage]) -> int:
        if not messages:
            return 0
        return self.reply_priming_overhead + sum(
            self.message_overhead
            + self.count(message.role)
            + self.count(message.content)
            for message in messages
        )


class ConservativeEstimator(_MessageCountingMixin):
    """对未知 tokenizer 使用偏保守、完全确定性的 Unicode 估算。"""

    def count(self, text: str) -> int:
        value = str(text or "")
        if not value:
            return 0
        weighted = sum(1.0 if ord(char) > 127 else 0.25 for char in value)
        return max(1, math.ceil(weighted))


class TiktokenEstimator(_MessageCountingMixin):
    def __init__(self, model: str = "", encoding_name: str = "cl100k_base") -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise TokenBudgetError("缺少 tiktoken 依赖") from exc
        try:
            self.encoding = tiktoken.encoding_for_model(model) if model else None
        except KeyError:
            self.encoding = None
        if self.encoding is None:
            self.encoding = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self.encoding.encode(str(text or "")))


class TokenizerJsonEstimator(_MessageCountingMixin):
    def __init__(self, tokenizer_path: str) -> None:
        path = Path(tokenizer_path)
        if not tokenizer_path or not path.is_file():
            raise TokenBudgetError(f"tokenizer JSON 不存在: {tokenizer_path or '<empty>'}")
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise TokenBudgetError("缺少 tokenizers 依赖") from exc
        try:
            self.tokenizer = Tokenizer.from_file(str(path))
        except Exception as exc:
            raise TokenBudgetError(f"无法加载 tokenizer JSON: {path}") from exc

    def count(self, text: str) -> int:
        value = str(text or "")
        return len(self.tokenizer.encode(value).ids) if value else 0


class SafetyAdjustedEstimator:
    def __init__(self, inner: TokenEstimator, multiplier: float) -> None:
        if multiplier < 1.0:
            raise TokenBudgetError("token 估算安全乘数不能小于 1")
        self.inner = inner
        self.multiplier = multiplier

    def count(self, text: str) -> int:
        raw = self.inner.count(text)
        return math.ceil(raw * self.multiplier) if raw else 0

    def count_messages(self, messages: Sequence[AiMessage]) -> int:
        raw = self.inner.count_messages(messages)
        return math.ceil(raw * self.multiplier) if raw else 0


class TokenEstimatorRegistry:
    def __init__(self, settings) -> None:
        self.settings = settings

    def for_model(self, capabilities: ModelCapabilities) -> SafetyAdjustedEstimator:
        kind = capabilities.tokenizer_kind.strip().lower()
        if kind == "tiktoken":
            inner: TokenEstimator = TiktokenEstimator(capabilities.model)
        elif kind in {"tokenizer_json", "huggingface", "qwen"}:
            inner = TokenizerJsonEstimator(capabilities.tokenizer_path)
        else:
            inner = ConservativeEstimator()
        multiplier = float(
            getattr(self.settings, "model_token_estimator_safety_multiplier", 1.05)
        )
        return SafetyAdjustedEstimator(inner, multiplier)


def calculate_input_budget(
    capabilities: ModelCapabilities,
    requested_output_tokens: int,
    *,
    reserve: int,
    margin_ratio: float,
) -> int:
    if requested_output_tokens <= 0:
        raise TokenBudgetError("requested_output_tokens 必须大于 0")
    if reserve < 0:
        raise TokenBudgetError("reserve 不能小于 0")
    if not 0 <= margin_ratio < 1:
        raise TokenBudgetError("margin_ratio 必须位于 [0, 1)")
    budget = int(
        capabilities.context_window * (1.0 - margin_ratio)
        - requested_output_tokens
        - reserve
    )
    if budget <= 0:
        raise TokenBudgetError("模型上下文窗口不足以容纳输出与恢复预留")
    return budget
