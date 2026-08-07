"""低基数 Prometheus 指标与管理员聚合快照。"""

from __future__ import annotations

import math
import threading
from collections import Counter as ValueCounter, deque
from typing import Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram

from app.services.model_trace import ModelTraceEvent


class RuntimeMetrics:
    _LABELS = {
        "model_calls": ("provider", "model", "status", "risk", "release"),
        "recovery": ("provider", "model", "reason"),
        "cloud_egress": ("provider", "model", "status", "risk"),
        "context_tokens": ("provider", "model", "stage"),
        "compaction": ("layer", "reason"),
        "memory": ("operation", "status"),
    }

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.model_calls = Counter(
            "mindbridge_model_calls_total",
            "模型调用终态数量",
            self._LABELS["model_calls"],
            registry=self.registry,
        )
        self.recovery = Counter(
            "mindbridge_model_recovery_total",
            "模型恢复与路由事件",
            self._LABELS["recovery"],
            registry=self.registry,
        )
        self.cloud_egress = Counter(
            "mindbridge_cloud_egress_total",
            "受控云出域终态",
            self._LABELS["cloud_egress"],
            registry=self.registry,
        )
        self.context_tokens = Histogram(
            "mindbridge_context_tokens",
            "上下文规划 token 数",
            self._LABELS["context_tokens"],
            buckets=(128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768),
            registry=self.registry,
        )
        self.compaction = Counter(
            "mindbridge_context_compaction_total",
            "上下文压缩动作",
            self._LABELS["compaction"],
            registry=self.registry,
        )
        self.memory = Counter(
            "mindbridge_memory_operations_total",
            "记忆提取与整合终态",
            self._LABELS["memory"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "mindbridge_model_latency_ms",
            "模型终态延迟毫秒",
            ("provider", "model", "status"),
            buckets=(10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
            registry=self.registry,
        )
        self._lock = threading.Lock()
        self._model = ValueCounter()
        self._tokens = ValueCounter()
        self._context = ValueCounter()
        self._memory = ValueCounter()
        self._latencies: deque[float] = deque(maxlen=2000)

    @classmethod
    def declared_label_names(cls) -> set[str]:
        return {
            label
            for labels in cls._LABELS.values()
            for label in labels
        } | {"stage", "operation"}

    def record(self, event: ModelTraceEvent) -> None:
        kind = self._label(event.kind, 32).lower()
        provider = self._label(event.provider or "unknown", 64)
        model = self._label(event.model or "unknown", 128)
        risk = self._label(event.risk_level or "LOW", 32)
        release = self._label(event.prompt_release or "unknown", 64)
        if "retry" in kind or "fallback" in kind or "escalat" in kind or "continuation" in kind:
            reason = self._label(event.error_code or kind, 64)
            self.recovery.labels(provider, model, reason).inc()
            with self._lock:
                self._model["retries"] += 1
                if "fallback" in kind:
                    self._model["fallback"] += 1
            return
        if kind not in {"success", "failure", "stream_success", "stream_failure"}:
            return
        status = "success" if kind.endswith("success") else "failure"
        self.model_calls.labels(provider, model, status, risk, release).inc()
        self.latency.labels(provider, model, status).observe(max(0.0, event.latency_ms))
        if event.cloud_egress:
            self.cloud_egress.labels(provider, model, status, risk).inc()
        with self._lock:
            self._model["total"] += 1
            self._model[status] += 1
            self._model["cloudEgress"] += int(event.cloud_egress)
            self._tokens["input"] += max(0, int(event.input_tokens or 0))
            self._tokens["output"] += max(0, int(event.output_tokens or 0))
            self._latencies.append(max(0.0, float(event.latency_ms or 0.0)))

    def record_context_plan(
        self,
        *,
        provider: str,
        model: str,
        tokens_before: int,
        tokens_after: int,
        actions: Iterable[tuple[str, str]],
    ) -> None:
        safe_provider = self._label(provider or "unknown", 64)
        safe_model = self._label(model or "unknown", 128)
        before = max(0, int(tokens_before or 0))
        after = max(0, int(tokens_after or 0))
        self.context_tokens.labels(safe_provider, safe_model, "before").observe(before)
        self.context_tokens.labels(safe_provider, safe_model, "after").observe(after)
        action_count = 0
        for layer, reason in actions:
            self.compaction.labels(
                self._label(layer or "unknown", 32),
                self._label(reason or "unknown", 64),
            ).inc()
            action_count += 1
        with self._lock:
            self._context["plans"] += 1
            self._context["tokensBefore"] += before
            self._context["tokensAfter"] += after
            self._context["actions"] += action_count

    def record_memory(self, operation: str, status: str, count: int = 1) -> None:
        safe_operation = self._label(operation or "unknown", 32)
        safe_status = self._label(status or "unknown", 32)
        amount = max(0, int(count or 0))
        self.memory.labels(safe_operation, safe_status).inc(amount)
        with self._lock:
            self._memory[f"{safe_operation}:{safe_status}"] += amount

    def snapshot(self) -> dict:
        with self._lock:
            latencies = sorted(self._latencies)
            return {
                "modelCalls": dict(self._model),
                "tokens": dict(self._tokens),
                "latencyMs": {
                    "samples": len(latencies),
                    "p50": self._percentile(latencies, 0.50),
                    "p95": self._percentile(latencies, 0.95),
                    "p99": self._percentile(latencies, 0.99),
                },
                "context": dict(self._context),
                "memory": dict(self._memory),
            }

    @staticmethod
    def _label(value: str, limit: int) -> str:
        normalized = " ".join(str(value or "unknown").split())
        return normalized[:limit] or "unknown"

    @staticmethod
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))
        return round(float(values[index]), 3)


_RUNTIME_METRICS = RuntimeMetrics()


def get_runtime_metrics() -> RuntimeMetrics:
    return _RUNTIME_METRICS
