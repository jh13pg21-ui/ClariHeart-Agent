# Model Gateway 与错误恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Ollama 主路由、OpenAI-compatible 受控降级的统一模型出口，并实现类型化错误、独立重试预算、输出截断和流式恢复。

**Architecture:** 新增 `app/llm` 包，Provider Adapter 只负责协议转换，`RecoveryOrchestrator` 负责单次调用生命周期，`ModelGateway` 负责路由与隐私守卫。现有 `AiClient` 保留兼容方法但内部转调 Gateway，使所有既有调用自动进入统一恢复边界。

**Tech Stack:** Python 3.12、httpx、FastAPI、Pydantic Settings、unittest/pytest、Ollama Chat API、OpenAI-compatible Chat Completions API。

## Global Constraints

- Ollama 为主模型，OpenAI-compatible 为备用模型。
- LOW/MEDIUM 只允许脱敏、最小必要上下文出域；HIGH 永远禁止云降级。
- 保持 `AiClient.complete()`、`AiClient.stream()` 和现有 SSE 行为向后兼容。
- 重试必须受 deadline 限制，认证和永久错误不重试。
- 所有测试先失败、再实现、再通过；每个任务独立提交。
- 不保存完整 Prompt 或未脱敏用户输入。

---

### Task 1: 模型调用契约与能力注册表

**Files:**
- Create: `app/llm/__init__.py`
- Create: `app/llm/contracts.py`
- Create: `app/llm/capabilities.py`
- Modify: `app/core/config.py`
- Test: `tests/test_model_capabilities.py`

**Interfaces:**
- Produces: `ModelRequest`、`ModelResult`、`ModelStreamEvent`、`ModelCapabilities`。
- Produces: `ModelCapabilitiesRegistry.for_model(provider: str, model: str) -> ModelCapabilities`。
- Consumes: `RiskLevel`、`AiMessage`。

- [ ] **Step 1: Write the failing contract tests**

```python
def test_registry_uses_explicit_ollama_capabilities():
    settings = SimpleNamespace(
        ollama_model="qwen-local",
        openai_model="cloud-model",
        model_context_window_default=8192,
        model_ollama_context_window=32768,
        model_openai_context_window=128000,
        ai_max_tokens=512,
    )
    capability = ModelCapabilitiesRegistry(settings).for_model("ollama", "qwen-local")
    assert capability.context_window == 32768
    assert capability.cloud is False

def test_model_request_defaults_to_no_cloud_egress():
    request = ModelRequest(
        request_id="r1", agent_name="SafetyAgent", task_name="risk",
        risk_level=RiskLevel.HIGH, messages=(), preferred_provider="ollama",
        preferred_model="qwen-local", max_output_tokens=512,
    )
    assert request.cloud_egress_allowed is False
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_model_capabilities.py`

Expected: FAIL because `app.llm` and the contracts do not exist.

- [ ] **Step 3: Implement immutable contracts and registry**

```python
@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    context_window: int
    default_output_tokens: int
    maximum_output_tokens: int
    cloud: bool
    supports_stream: bool = True

class ModelCapabilitiesRegistry:
    def for_model(self, provider: str, model: str) -> ModelCapabilities:
        prefix = "model_openai" if provider == "openai" else "model_ollama"
        return ModelCapabilities(
            provider=provider, model=model,
            context_window=int(getattr(self.settings, f"{prefix}_context_window")),
            default_output_tokens=int(self.settings.ai_max_tokens),
            maximum_output_tokens=int(getattr(self.settings, f"{prefix}_max_output_tokens")),
            cloud=provider == "openai",
        )
```

Add settings with concrete defaults: default context 8192, Ollama context 32768/output 4096, OpenAI context 128000/output 16384, recovery reserve 1024, safety margin ratio 0.10.

- [ ] **Step 4: Run tests and existing AI tests**

Run: `python -m pytest -q tests/test_model_capabilities.py tests/test_async_ai.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm app/core/config.py tests/test_model_capabilities.py
git commit -m "feat: define model gateway contracts and capabilities"
```

### Task 2: 类型化 Provider 错误

**Files:**
- Create: `app/llm/errors.py`
- Test: `tests/test_model_errors.py`

**Interfaces:**
- Produces: `ModelErrorCode`、`ModelError`、`classify_http_error()`、`classify_transport_error()`。
- Consumes: `httpx.Response`、`BaseException`。

- [ ] **Step 1: Write failing classification tests**

```python
@pytest.mark.parametrize("status,code,retryable", [
    (429, ModelErrorCode.RATE_LIMITED, True),
    (529, ModelErrorCode.OVERLOADED, True),
    (401, ModelErrorCode.AUTHENTICATION, False),
    (400, ModelErrorCode.PERMANENT, False),
])
def test_http_status_is_classified(status, code, retryable):
    response = httpx.Response(status, headers={"Retry-After": "3"}, request=httpx.Request("POST", "http://model"))
    error = classify_http_error(response, "ollama", "model")
    assert error.code == code
    assert error.retryable is retryable
    assert error.retry_after_seconds == 3.0

def test_prompt_too_long_payload_is_not_generic_400():
    response = httpx.Response(400, json={"error": {"code": "context_length_exceeded"}}, request=httpx.Request("POST", "http://model"))
    assert classify_http_error(response, "openai", "model").code == ModelErrorCode.PROMPT_TOO_LONG
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_model_errors.py`

Expected: FAIL because error types are missing.

- [ ] **Step 3: Implement stable error mapping**

Map 408/timeout to `TIMEOUT`, 429 to `RATE_LIMITED`, 502/503/504/529 to `OVERLOADED`, authentication status to `AUTHENTICATION`, context markers to `PROMPT_TOO_LONG`, malformed success payload to `INVALID_RESPONSE`, and `httpx.NetworkError` to `NETWORK`. Sanitize response text to 500 characters.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_model_errors.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/errors.py tests/test_model_errors.py
git commit -m "feat: classify model provider failures"
```

### Task 3: Ollama 与 OpenAI Provider Adapter

**Files:**
- Create: `app/llm/providers.py`
- Test: `tests/test_model_providers.py`

**Interfaces:**
- Produces: `ModelProvider` Protocol、`OllamaProvider`、`OpenAICompatibleProvider`。
- Consumes: `ModelRequest`。
- Produces: `ModelResult`，保留 finish reason、usage、request ID 和 partial 状态。

- [ ] **Step 1: Write failing Provider tests with MockTransport**

```python
async def test_ollama_result_preserves_usage_and_done_reason():
    payload = {"message": {"content": "ok"}, "done_reason": "stop", "prompt_eval_count": 42, "eval_count": 7}
    provider = OllamaProvider(client_with_json(payload), "http://ollama")
    result = await provider.complete(model_request("ollama"))
    assert (result.text, result.finish_reason) == ("ok", "stop")
    assert (result.input_tokens, result.output_tokens) == (42, 7)

async def test_openai_result_preserves_finish_reason_and_request_id():
    payload = {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}
    provider = OpenAICompatibleProvider(client_with_json(payload, headers={"x-request-id": "req-1"}), "http://cloud", "key")
    result = await provider.complete(model_request("openai"))
    assert result.finish_reason == "length"
    assert result.partial is True
    assert result.provider_request_id == "req-1"
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_model_providers.py`

Expected: FAIL because Provider adapters do not exist.

- [ ] **Step 3: Implement adapters**

Use `httpx.AsyncClient`, `response.raise_for_status()` only after mapping the response through `classify_http_error()`. Validate all required response paths and raise `ModelError(INVALID_RESPONSE)` when absent. Streaming adapters emit `ModelStreamEvent(kind="token"|"usage"|"done")` and map interrupted iteration to `STREAM_INTERRUPTED`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_model_providers.py tests/test_model_errors.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/providers.py tests/test_model_providers.py
git commit -m "feat: add typed Ollama and OpenAI providers"
```

### Task 4: 云端出域策略

**Files:**
- Create: `app/llm/egress.py`
- Test: `tests/test_cloud_egress_policy.py`

**Interfaces:**
- Produces: `CloudEgressDecision`、`CloudEgressPolicy.evaluate(request) -> CloudEgressDecision`。
- Consumes: `ModelRequest`、`RiskLevel`、`PrivacySanitizer`。

- [ ] **Step 1: Write failing policy tests**

```python
def test_high_risk_never_allows_cloud():
    decision = CloudEgressPolicy().evaluate(request(risk=RiskLevel.HIGH, cloud_egress_allowed=True))
    assert decision.allowed is False
    assert decision.reason == "high_risk_must_remain_local"

def test_low_risk_requires_explicit_sanitized_request():
    assert CloudEgressPolicy().evaluate(request(risk=RiskLevel.LOW, cloud_egress_allowed=False)).allowed is False
    assert CloudEgressPolicy().evaluate(request(risk=RiskLevel.LOW, cloud_egress_allowed=True, sanitized=True)).allowed is True
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_cloud_egress_policy.py`

Expected: FAIL because policy does not exist.

- [ ] **Step 3: Implement fail-closed policy**

The request contract gains `sanitized: bool` and `context_section_ids`. HIGH always denies. LOW/MEDIUM require `cloud_egress_allowed and sanitized`; an empty cloud model/API key is a route-unavailable decision, not permission.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_cloud_egress_policy.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/contracts.py app/llm/egress.py tests/test_cloud_egress_policy.py
git commit -m "feat: enforce risk-aware cloud egress"
```

### Task 5: RecoveryOrchestrator 瞬态错误与 deadline

**Files:**
- Create: `app/llm/recovery.py`
- Test: `tests/test_recovery_orchestrator.py`

**Interfaces:**
- Produces: `RecoveryPolicy`、`RecoveryState`、`RecoveryOrchestrator.complete()`。
- Consumes: primary/fallback `ModelProvider`、`CloudEgressPolicy`、injectable `sleep` 和随机源。

- [ ] **Step 1: Write failing retry tests**

```python
async def test_retry_after_wins_over_exponential_delay():
    provider = SequenceProvider([model_error(RATE_LIMITED, retry_after=3), model_result("ok")])
    sleeps = []
    result = await orchestrator(provider, sleep=sleeps.append).complete(low_request())
    assert result.text == "ok"
    assert sleeps == [3.0]

async def test_deadline_stops_retry_before_sleeping_past_budget():
    provider = SequenceProvider([model_error(OVERLOADED)] * 5)
    with pytest.raises(ModelError) as raised:
        await orchestrator(provider, deadline_seconds=0.4).complete(low_request())
    assert raised.value.code == ModelErrorCode.OVERLOADED
    assert provider.calls == 1
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_recovery_orchestrator.py`

Expected: FAIL because orchestrator does not exist.

- [ ] **Step 3: Implement independent transient budgets**

Implement `min(0.5 * 2 ** (attempt - 1), 32.0) + jitter`, Retry-After precedence, deadline checks before each attempt and sleep, reset consecutive overload on success, and no retry for authentication/permanent/content-policy errors. Inject clock/sleep/random for deterministic tests.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_recovery_orchestrator.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/recovery.py tests/test_recovery_orchestrator.py
git commit -m "feat: add deadline-aware model recovery"
```

### Task 6: 模型降级与 HIGH fail-closed

**Files:**
- Modify: `app/llm/recovery.py`
- Create: `app/llm/gateway.py`
- Test: `tests/test_model_gateway_routing.py`

**Interfaces:**
- Produces: `ModelGateway.complete(request) -> ModelResult`。
- Consumes: Provider map、RecoveryOrchestrator、CloudEgressPolicy。

- [ ] **Step 1: Write failing routing tests**

```python
async def test_low_risk_switches_to_cloud_after_local_overload_budget():
    local = SequenceProvider([model_error(OVERLOADED)] * 3)
    cloud = SequenceProvider([model_result("cloud", provider="openai")])
    result = await gateway(local, cloud).complete(low_request(sanitized=True, cloud=True))
    assert result.provider == "openai"
    assert cloud.calls == 1

async def test_high_risk_never_invokes_cloud():
    local = SequenceProvider([model_error(OVERLOADED)] * 3)
    cloud = SequenceProvider([model_result("forbidden")])
    with pytest.raises(ModelError):
        await gateway(local, cloud).complete(high_request())
    assert cloud.calls == 0
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_model_gateway_routing.py`

Expected: FAIL because Gateway routing is missing.

- [ ] **Step 3: Implement routing**

Only NETWORK/TIMEOUT/OVERLOADED/RATE_LIMITED exhaustion can request fallback. Before fallback, re-run CloudEgressPolicy. PROMPT_TOO_LONG remains on the same route for ContextPlanner to handle. Emit structured recovery events through an injected callback.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_model_gateway_routing.py tests/test_recovery_orchestrator.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/gateway.py app/llm/recovery.py tests/test_model_gateway_routing.py
git commit -m "feat: route model calls with privacy-safe fallback"
```

### Task 7: 输出截断与有限续写

**Files:**
- Modify: `app/llm/recovery.py`
- Test: `tests/test_output_token_recovery.py`

**Interfaces:**
- Extends: `RecoveryOrchestrator.complete()` 对 finish reason `length/max_tokens` 的处理。
- Produces: `ModelResult.partial=True` when bounded continuation cannot finish.

- [ ] **Step 1: Write failing truncation tests**

```python
async def test_first_truncation_escalates_without_appending_partial_text():
    provider = CapturingProvider([truncated("half"), model_result("complete")])
    result = await orchestrator(provider).complete(low_request(max_output_tokens=512))
    assert result.text == "complete"
    assert provider.requests[1].messages == provider.requests[0].messages
    assert provider.requests[1].max_output_tokens > 512

async def test_diminishing_continuations_return_explicit_partial():
    provider = SequenceProvider([truncated("a"), truncated("b"), truncated("b")])
    result = await orchestrator(provider, max_continuations=2).complete(low_request_at_max_output())
    assert result.partial is True
    assert result.finish_reason == "recovery_limit"
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_output_token_recovery.py`

Expected: FAIL because finish reason does not drive recovery.

- [ ] **Step 3: Implement escalation and diminishing-return guard**

Escalate once up to `ModelCapabilities.maximum_output_tokens`. Only after reaching that ceiling append reviewed partial text plus the fixed continuation instruction. Stop after two continuations, or when normalized new output is under 128 characters/repeats the prior continuation above 90% similarity.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_output_token_recovery.py tests/test_recovery_orchestrator.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/recovery.py tests/test_output_token_recovery.py
git commit -m "feat: recover bounded model output truncation"
```

### Task 8: 流式中断恢复

**Files:**
- Modify: `app/llm/recovery.py`
- Modify: `app/llm/gateway.py`
- Test: `tests/test_stream_recovery.py`

**Interfaces:**
- Produces: `ModelGateway.stream(request) -> AsyncIterator[ModelStreamEvent]`。
- Accepts: `on_replace(text)` callback for already-released LOW-risk content。

- [ ] **Step 1: Write failing stream recovery tests**

```python
async def test_stream_interruption_before_release_retries_without_duplicate_tokens():
    provider = StreamingSequenceProvider([interrupted_stream(["半"]), completed_stream(["完整"] )])
    events = [event async for event in gateway(provider).stream(low_stream_request())]
    assert token_text(events) == "完整"

async def test_high_risk_stream_is_buffered_and_never_falls_back_to_cloud():
    local = StreamingSequenceProvider([interrupted_stream(["敏感片段"])] * 2)
    cloud = StreamingSequenceProvider([completed_stream(["forbidden"])])
    with pytest.raises(ModelError):
        _ = [event async for event in gateway(local, cloud).stream(high_stream_request())]
    assert cloud.calls == 0
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_stream_recovery.py`

Expected: FAIL because Gateway has no recovery-aware stream.

- [ ] **Step 3: Implement bounded stream recovery**

Buffer until the caller marks a segment reviewed. If interruption happens before release, retry the request once and discard the failed buffer. After LOW-risk release, emit a typed `replace_required` event carrying the final reviewed replacement; HIGH never releases tokens before full generation and safety review. Stream retry shares the request deadline and cloud egress policy.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_stream_recovery.py tests/test_output_token_recovery.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/llm/recovery.py app/llm/gateway.py tests/test_stream_recovery.py
git commit -m "feat: recover interrupted model streams safely"
```

### Task 9: AiClient 兼容迁移与回归

**Files:**
- Modify: `app/services/ai.py`
- Modify: `app/services/agent_models.py`
- Modify: `app/agents/autonomous.py`
- Test: `tests/test_async_ai.py`
- Test: `tests/test_response_candidate.py`
- Test: `tests/test_gateway_compatibility.py`

**Interfaces:**
- `AiClient.complete()` remains `async -> str` but calls `ModelGateway.complete()`。
- Adds: `AiClient.complete_result() -> ModelResult`。
- `AiClient.stream()` delegates to Gateway stream while preserving string chunks。

- [ ] **Step 1: Write failing compatibility tests**

```python
async def test_ai_client_complete_uses_gateway_and_preserves_string_api():
    gateway = FakeGateway(model_result("ok"))
    client = AiClient(settings(), gateway=gateway)
    assert await client.complete([AiMessage(role="user", content="hi")]) == "ok"
    assert gateway.requests[0].agent_name == "legacy"

async def test_response_agent_passes_actual_risk_to_gateway():
    client = CapturingAiClient("safe")
    await ResponseAgent(services(client)).act(response_task(), high_risk_board())
    assert client.risk_levels == [RiskLevel.HIGH]
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_gateway_compatibility.py tests/test_response_candidate.py`

Expected: FAIL because AiClient has no Gateway injection or risk argument.

- [ ] **Step 3: Implement compatibility facade**

Build providers once per AiClient, pass agent/task/risk metadata, and remove direct HTTP implementation from `AiClient`. Replace ResponseAgent's broad Provider catch with handling of exhausted `ModelError`; publish existing safe fallback with explicit `generationStatus`, `failureCode` and `route` metadata.

- [ ] **Step 4: Run targeted regression**

Run: `python -m pytest -q tests/test_async_ai.py tests/test_gateway_compatibility.py tests/test_response_candidate.py tests/test_async_agent_runtime.py tests/test_privacy_and_assessment.py`

Expected: PASS.

- [ ] **Step 5: Run repository regression excluding external PDF baseline**

Run: `python -m pytest -q --ignore=tests/rag_ingestion/test_evaluation.py`

Expected: PASS with the pre-existing deprecation warnings only.

- [ ] **Step 6: Commit**

```bash
git add app/services/ai.py app/services/agent_models.py app/agents/autonomous.py tests
git commit -m "refactor: route agent model calls through gateway"
```
