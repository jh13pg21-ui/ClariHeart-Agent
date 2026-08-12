"""长期记忆的确定性、可解释复合排序。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class RankedMemory:
    memory: Any
    score: float
    relevance: float
    components: dict[str, float]


class MemoryRanker:
    def __init__(self, text_resolver: Callable[[str], str] | None = None) -> None:
        self.text_resolver = text_resolver or (lambda value: str(value or ""))

    def score(
        self,
        memory,
        query: str,
        now: datetime,
        *,
        semantic_match: bool = False,
    ) -> float:
        return self._ranked(memory, query, now, semantic_match=semantic_match).score

    def rank(
        self,
        memories: Iterable[Any],
        query: str,
        *,
        now: datetime | None = None,
        semantic_ids: set[str] | None = None,
    ) -> list[RankedMemory]:
        reference = now or datetime.utcnow()
        semantic = semantic_ids or set()
        ranked = [
            self._ranked(
                memory,
                query,
                reference,
                semantic_match=str(getattr(memory, "public_id", "")) in semantic,
            )
            for memory in memories
        ]
        ranked.sort(
            key=lambda item: (
                item.score,
                self._timestamp(getattr(item.memory, "updated_at", None)),
                int(getattr(item.memory, "id", 0) or 0),
            ),
            reverse=True,
        )
        return ranked

    def _ranked(
        self,
        memory,
        query: str,
        now: datetime,
        *,
        semantic_match: bool,
    ) -> RankedMemory:
        query_tokens = self._tokens(query)
        body = self.text_resolver(str(getattr(memory, "body", "") or ""))
        memory_tokens = self._tokens(
            " ".join(
                (
                    str(getattr(memory, "name", "") or ""),
                    str(getattr(memory, "description", "") or ""),
                    body,
                )
            )
        )
        overlap = len(query_tokens & memory_tokens)
        lexical = min(1.0, overlap / max(1, min(6, len(query_tokens))))
        semantic = 1.0 if semantic_match else 0.0
        confidence = min(1.0, max(0.0, float(getattr(memory, "confidence", 0.5) or 0.0)))
        age_days = self._age_days(now, getattr(memory, "updated_at", None))
        recency = math.exp(-age_days / 180.0)
        confirmations = min(
            1.0,
            math.log1p(max(0, int(getattr(memory, "confirmation_count", 0) or 0)))
            / math.log(6),
        )
        usage = min(
            1.0,
            math.log1p(max(0, int(getattr(memory, "usage_count", 0) or 0)))
            / math.log(11),
        )
        conflict_penalty = (
            1.5
            if str(getattr(memory, "status", "ACTIVE")).upper() != "ACTIVE"
            else 0.0
        )
        expires_at = getattr(memory, "expires_at", None)
        expired_penalty = 2.0 if expires_at is not None and self._is_before(expires_at, now) else 0.0
        components = {
            "lexical": lexical,
            "semantic": semantic,
            "confidence": confidence,
            "recency": recency,
            "confirmation": confirmations,
            "usage": usage,
            "conflictPenalty": conflict_penalty,
            "expiredPenalty": expired_penalty,
        }
        score = (
            0.45 * lexical
            + 0.25 * semantic
            + 0.15 * confidence
            + 0.08 * recency
            + 0.05 * confirmations
            + 0.02 * usage
            - conflict_penalty
            - expired_penalty
        )
        return RankedMemory(
            memory=memory,
            score=round(score, 8),
            relevance=max(lexical, semantic),
            components=components,
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = re.sub(r"\s+", "", str(text or "").lower())
        latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
        grams = {
            chinese[index : index + 2]
            for index in range(max(0, len(chinese) - 1))
        }
        return latin | grams

    @staticmethod
    def _age_days(now: datetime, value: datetime | None) -> float:
        if value is None:
            return 3650.0
        left, right = MemoryRanker._comparable(now, value)
        return max(0.0, (left - right).total_seconds() / 86400.0)

    @staticmethod
    def _is_before(value: datetime, now: datetime) -> bool:
        left, right = MemoryRanker._comparable(value, now)
        return left <= right

    @staticmethod
    def _comparable(left: datetime, right: datetime) -> tuple[datetime, datetime]:
        if (left.tzinfo is None) != (right.tzinfo is None):
            return left.replace(tzinfo=None), right.replace(tzinfo=None)
        return left, right

    @staticmethod
    def _timestamp(value: datetime | None) -> float:
        if value is None:
            return 0.0
        try:
            return value.timestamp()
        except (OSError, OverflowError, ValueError):
            return 0.0
