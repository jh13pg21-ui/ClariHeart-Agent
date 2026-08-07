# Memory V2、可观测性与灰度发布 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为会话摘要增加并发安全水位线，为长期记忆增加证据、置信度、状态和巩固，并补齐模型调用、Prompt、压缩和云出域的隐私安全可观测性。

**Architecture:** 会话摘要继续使用现有权威检查点，通过 scheduled watermark 和 compare-and-set 消除重复任务；长期记忆采用追加证据与 supersede 状态演进；Consolidator 通过 Outbox/Celery 低频运行。Trace 和 Prometheus 指标只保存脱敏元数据，发布通过 feature flag、shadow 和双写分阶段完成。

**Tech Stack:** Python 3.12、SQLAlchemy、Alembic、MySQL、Redis、Celery、Transactional Outbox、prometheus-client、pytest。

## Global Constraints

- HIGH 风险原文、联系方式、诊断和风险等级不得写入长期记忆。
- 新长期记忆必须携带合法来源消息 ID；旧数据以 legacy 状态兼容迁移。
- 巩固不物理删除系统判断为过期或冲突的记忆；用户主动删除除外。
- 新增 trace 不保存 Prompt 原文、未脱敏输入或完整 artifact payload。
- 现有记忆 API、用户开关、数据删除和保留策略保持兼容。
- 所有异步任务幂等，水位线只单调前进。

---

### Task 1: Memory V2 与 Trace 数据迁移

**Files:**
- Modify: `app/models/entities.py`
- Create: `migrations/versions/0010_memory_v2_and_model_traces.py`
- Modify: `migrations/legacy_schema.py`
- Test: `tests/test_memory_v2_migration.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_legacy_schema_contract.py`

**Interfaces:**
- Extends: `ConversationMemorySummary` with scheduled watermark and budget metadata。
- Extends: `LongTermMemory` with status/evidence/confidence/version/usage fields。
- Produces: `ModelCallTrace`、`MemoryConsolidationRun` entities。

- [ ] **Step 1: Write failing migration tests**

```python
def test_legacy_memory_is_migrated_to_active_legacy_state(upgraded_db):
    row = upgraded_db.execute(text("select status, confidence, extraction_method from long_term_memories limit 1")).one()
    assert row.status == "ACTIVE"
    assert row.confidence == pytest.approx(0.5)
    assert row.extraction_method == "legacy"

def test_trace_tables_do_not_have_prompt_content_columns():
    columns = inspected_columns("model_call_traces")
    assert "prompt" not in columns
    assert "messages" not in columns
    assert {"prompt_manifest_hash", "context_plan_hash", "input_tokens"}.issubset(columns)
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_memory_v2_migration.py tests/test_migrations.py`

Expected: FAIL because migration and entities do not exist.

- [ ] **Step 3: Implement additive schema**

Add concrete defaults: status ACTIVE, confidence 0.5, evidence `[]`, extraction method legacy, usage/confirmation zero. Add nullable supersedes FK and timestamps. Add indexes on `(user_id,status,updated_at)` and consolidation `(user_id,status,created_at)`. Keep downgrade safe for the new fields/tables only.

- [ ] **Step 4: Run migration tests**

Run: `python -m pytest -q tests/test_memory_v2_migration.py tests/test_migrations.py tests/test_legacy_schema_contract.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models/entities.py migrations tests/test_memory_v2_migration.py tests/test_migrations.py tests/test_legacy_schema_contract.py
git commit -m "feat: migrate memory evidence and model trace schema"
```

### Task 2: 会话摘要调度水位线与摘要冻结修复

**Files:**
- Modify: `app/services/memory.py`
- Modify: `app/services/conversation_summary.py`
- Modify: `app/agents/harness.py`
- Test: `tests/test_memory_compaction.py`
- Test: `tests/test_structured_conversation_summary.py`
- Test: `tests/test_summary_scheduling.py`

**Interfaces:**
- Produces: `ConversationSummaryService.reserve_refresh(session, assistant_message) -> bool`。
- Produces: `ConversationSummaryService.ensure_through(session, through_message_id, reason) -> ConversationMemorySummary`。
- Replaces: separate `should_schedule_refresh()` check followed by unreserved Outbox creation。

- [ ] **Step 1: Write failing regression tests**

```python
def test_deterministic_summary_keeps_new_information_after_reaching_limit():
    state = saturated_summary_state()
    advanced = state.advance([AiMessage(role="user", content="新的重要事项：明天面试")], settings())
    assert "明天面试" in advanced.summary

def test_second_assistant_does_not_schedule_same_unprocessed_range(db):
    service = summary_service(db)
    assert service.reserve_refresh(session, assistant_12) is True
    assert service.reserve_refresh(session, assistant_14) is False
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_memory_compaction.py tests/test_summary_scheduling.py`

Expected: FAIL because old summary clips the suffix and no scheduled watermark exists.

- [ ] **Step 3: Implement rolling deterministic summary and reservation**

When merging deterministic summaries, reserve at least half of the character budget for the newest addition and trim the oldest summary prefix. Reservation updates `scheduled_through_message_id` in the same transaction as the Outbox event. Worker success advances `through_message_id`; terminal failure releases or leaves a retryable reservation according to event status.

- [ ] **Step 4: Implement synchronous ensure_through**

Use `SELECT FOR UPDATE`, re-read the current watermark, compact only complete refresh batches, and make concurrent calls return the already-advanced record. Record reason, Prompt release and token usage.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_memory_compaction.py tests/test_structured_conversation_summary.py tests/test_summary_scheduling.py tests/test_long_term_memory_outbox.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/memory.py app/services/conversation_summary.py app/agents/harness.py tests
git commit -m "fix: make summary refresh monotonic and concurrency-safe"
```

### Task 3: 证据化长期记忆写入

**Files:**
- Modify: `app/services/long_term_memory.py`
- Modify: `app/schemas/dtos.py`
- Test: `tests/test_long_term_memory_v2.py`
- Test: `tests/test_long_term_memory_api.py`

**Interfaces:**
- Adds: `MemoryCandidate` dataclass with evidence IDs and confidence。
- Changes internal: `upsert_candidate(user_id, source_session_id, candidate) -> LongTermMemory | None`。
- Keeps public: existing memory response fields, with optional V2 metadata additions only。

- [ ] **Step 1: Write failing evidence tests**

```python
def test_new_memory_without_valid_user_evidence_is_rejected():
    candidate = MemoryCandidate("PREFERENCE", "回复风格", "简洁", "先给结论", (), 0.9)
    assert service.upsert_candidate(user.id, session.id, candidate) is None

async def test_extracted_memory_records_prompt_and_model_provenance():
    stored = await service.extract_from_messages(user.id, session.id, messages_with_ids())
    assert stored[0].evidence_message_ids == [user_message_id]
    assert stored[0].prompt_version == "memory_candidate_v2"
    assert stored[0].confidence >= 0.0
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_long_term_memory_v2.py`

Expected: FAIL because candidate evidence is not modeled.

- [ ] **Step 3: Implement V2 parsing and validation**

Feed message IDs into the registered extraction Prompt. Validate evidence IDs against USER messages in the source window, clamp confidence to 0..1, preserve Provider/Model/Prompt metadata, and reject unsafe candidates before hashing. Existing `upsert()` becomes a legacy adapter used only by tests/API migration and marks extraction method legacy.

- [ ] **Step 4: Implement supersede semantics**

For the same `(user,type,name)`, materially changed content creates a new ACTIVE row and marks the old row SUPERSEDED with `supersedes_memory_id`; exact content adds evidence/confirmation instead of creating a row. Do not overwrite historical body.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_long_term_memory_v2.py tests/test_long_term_memory.py tests/test_long_term_memory_api.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/long_term_memory.py app/schemas/dtos.py tests
git commit -m "feat: store evidence-backed versioned memories"
```

### Task 4: 长期记忆选择评分

**Files:**
- Create: `app/services/memory_ranking.py`
- Modify: `app/services/long_term_memory.py`
- Test: `tests/test_memory_ranking.py`

**Interfaces:**
- Produces: `MemoryRanker.score(memory, query, now) -> float`。
- Selects: ACTIVE non-expired memories only, except explicit user lookup may include SUPERSEDED metadata without body injection。

- [ ] **Step 1: Write failing ranking tests**

```python
def test_confirmed_relevant_memory_outranks_stale_conflict():
    scores = ranker.rank([confirmed_memory(), stale_conflicting_memory()], "秋招后端")
    assert scores[0].memory.id == confirmed_memory().id

def test_usage_does_not_raise_fact_confidence():
    memory = active_memory(confidence=0.6, usage_count=10)
    service.mark_used([memory])
    assert memory.confidence == 0.6
    assert memory.usage_count == 11
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_memory_ranking.py`

Expected: FAIL because ranker does not exist.

- [ ] **Step 3: Implement deterministic composite score**

Normalize keyword/semantic selector score, confidence, confirmation recency and usage as separate bounded terms; subtract expiration and conflict penalties. Stable-sort by score, updated time and ID. Update usage only after the selected memory is actually placed into a Context Plan.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_memory_ranking.py tests/test_long_term_memory_v2.py tests/test_conversation_memory_flow.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/memory_ranking.py app/services/long_term_memory.py tests/test_memory_ranking.py
git commit -m "feat: rank active memories with confidence and recency"
```

### Task 5: Memory Consolidator 与幂等 Worker

**Files:**
- Create: `app/services/memory_consolidation.py`
- Modify: `app/workers/tasks.py`
- Modify: `app/workers/outbox_publisher.py`
- Modify: `app/services/outbox.py`
- Modify: `app/core/config.py`
- Test: `tests/test_memory_consolidation.py`
- Test: `tests/test_memory_consolidation_worker.py`

**Interfaces:**
- Produces: `MemoryConsolidationService.should_schedule(user_id) -> bool`。
- Produces: `MemoryConsolidationService.consolidate(user_id) -> ConsolidationResult`。
- Adds Outbox event: `memory.consolidate` and Celery task `consolidate_long_term_memory`。

- [ ] **Step 1: Write failing four-gate tests**

```python
@pytest.mark.parametrize("gate", ["time", "count", "sessions", "lock"])
def test_consolidation_does_not_run_when_any_gate_is_closed(gate):
    service = consolidation_service_with_all_gates_open_except(gate)
    assert service.should_schedule(user.id) is False

async def test_consolidation_marks_superseded_and_expired_without_deleting():
    result = await service.consolidate(user.id)
    assert result.superseded == 1
    assert result.expired == 1
    assert db.query(LongTermMemory).count() == original_count
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_memory_consolidation.py`

Expected: FAIL because service does not exist.

- [ ] **Step 3: Implement gates and consolidation decisions**

Defaults: 24-hour time gate, 10 new ACTIVE memories, 5 modified sessions, and one RUNNING row per user as lock. The registered consolidation Prompt returns groups with action `MERGE|SUPERSEDE|EXPIRE|KEEP` and source memory IDs. Validate all IDs and apply status updates transactionally; invalid output results in no mutation.

- [ ] **Step 4: Implement Outbox/Worker idempotency**

Use existing ProcessedMessage claim pattern. Worker creates a run row, commits success atomically with memory state, and records sanitized failure. Duplicate events return ALREADY_PROCESSED.

- [ ] **Step 5: Run tests**

Run: `python -m pytest -q tests/test_memory_consolidation.py tests/test_memory_consolidation_worker.py tests/test_outbox_publisher.py tests/test_worker_tasks.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/memory_consolidation.py app/workers app/services/outbox.py app/core/config.py tests
git commit -m "feat: consolidate long-term memories asynchronously"
```

### Task 6: Model call trace 与隐私 redactor

**Files:**
- Create: `app/services/model_trace.py`
- Create: `app/services/trace_redaction.py`
- Modify: `app/services/trace.py`
- Modify: `app/llm/gateway.py`
- Test: `tests/test_model_trace.py`
- Test: `tests/test_trace_redaction.py`

**Interfaces:**
- Produces: `ModelTraceSink.record(event)` and SQLAlchemy implementation。
- Produces: `redact_collaboration_payload(value) -> Any`。
- Gateway emits start/retry/route/success/failure metadata events。

- [ ] **Step 1: Write failing privacy tests**

```python
def test_model_trace_never_persists_prompt_or_user_text(db):
    sink = SqlModelTraceSink(db)
    sink.record(trace_event(prompt="手机号13800138000", prompt_manifest_hash="abc"))
    row = db.query(ModelCallTrace).one()
    assert "13800138000" not in json.dumps(row_as_dict(row), ensure_ascii=False)
    assert row.prompt_manifest_hash == "abc"

def test_agent_trace_replaces_memory_body_with_source_metadata():
    redacted = redact_collaboration_payload({"longTermMemories": [{"id": "m1", "body": "敏感正文"}]})
    assert redacted == {"longTermMemories": [{"id": "m1", "body": "[REDACTED]"}]}
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_model_trace.py tests/test_trace_redaction.py`

Expected: FAIL because sink/redactor do not exist.

- [ ] **Step 3: Implement metadata-only trace**

Whitelist persisted fields rather than blacklist content. Redact artifact keys `history`, `modelHistory`, `body`, `content`, `skillContext`, `longTermMemoryContext` while preserving IDs, counts, statuses and hashes. Trace failure must never fail the user request.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_model_trace.py tests/test_trace_redaction.py tests/test_data_protection.py tests/test_admin_api.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/model_trace.py app/services/trace_redaction.py app/services/trace.py app/llm/gateway.py tests
git commit -m "feat: audit model recovery without storing prompt content"
```

### Task 7: Prometheus 指标与受限端点

**Files:**
- Create: `app/services/runtime_metrics.py`
- Modify: `app/main.py`
- Modify: `app/api/routes.py`
- Modify: `requirements.txt`
- Test: `tests/test_runtime_metrics.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Produces counters/histograms for model calls, retries, fallback, context tokens, compaction, Prompt release, memory extraction/consolidation and cloud egress。
- Adds: admin-protected `GET /api/admin/runtime-metrics` JSON summary; no public raw metrics endpoint。

- [ ] **Step 1: Write failing metrics tests**

```python
def test_runtime_metrics_do_not_use_user_or_session_as_labels():
    labels = RuntimeMetrics.declared_label_names()
    assert "user_id" not in labels
    assert "session_id" not in labels

def test_metrics_endpoint_requires_admin(client, student_cookies, admin_cookies):
    assert client.get("/api/admin/runtime-metrics", cookies=student_cookies).status_code == 403
    assert client.get("/api/admin/runtime-metrics", cookies=admin_cookies).status_code == 200
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_runtime_metrics.py tests/test_admin_api.py`

Expected: FAIL because metrics service/route is absent.

- [ ] **Step 3: Implement bounded-cardinality metrics**

Use only provider/model/status/reason/risk/release/layer labels. Connect Gateway, Planner, PromptAssembler and memory workers through small metric methods. Admin JSON returns aggregate counters and recent percentiles without raw request data.

- [ ] **Step 4: Run tests**

Run: `python -m pytest -q tests/test_runtime_metrics.py tests/test_admin_api.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/runtime_metrics.py app/main.py app/api/routes.py requirements.txt tests
git commit -m "feat: expose privacy-safe runtime reliability metrics"
```

### Task 8: 长上下文与混沌评测 Harness

**Files:**
- Create: `app/context_eval/__init__.py`
- Create: `app/context_eval/runner.py`
- Create: `app/context_eval/datasets/context-cases-v1.json`
- Create: `app/context_eval/faults.py`
- Create: `tests/test_context_eval.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `python -m app.context_eval.runner --json`。
- Reports: overflow recovery, required-section retention, token reduction, retry/fallback route, latency and cloud egress decisions。

- [ ] **Step 1: Write failing harness tests**

```python
def test_eval_dataset_covers_required_faults():
    dataset = load_dataset(dataset_path())
    assert {case.fault for case in dataset.cases}.issuperset({
        "none", "prompt_too_long", "429", "529", "timeout",
        "invalid_json", "stream_interrupted", "redis_unavailable",
    })

def test_high_risk_eval_never_routes_to_cloud():
    report = run_case(high_risk_local_failure_case())
    assert report.cloud_calls == 0
    assert report.final_status == "high_risk_local_fallback"
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_context_eval.py`

Expected: FAIL because evaluation package is missing.

- [ ] **Step 3: Implement deterministic fault adapters and report**

Use fake Providers and repositories, not live network. Dataset includes long conversation, oversized single message, many RAG chunks, multiple Skills, conflicting memories and lagging summary. JSON report includes pass/fail thresholds copied from the design acceptance criteria.

- [ ] **Step 4: Run harness and regression**

Run: `python -m pytest -q tests/test_context_eval.py`

Run: `python -m app.context_eval.runner --json`

Expected: tests PASS and harness exits 0 with no HIGH cloud calls or required-section loss.

- [ ] **Step 5: Update README**

Document configuration, architecture flow, privacy routing, failure states, evaluation command and interpretation of metrics. Describe the system as a MindBridge-specific production design rather than a Claude Code copy.

- [ ] **Step 6: Run repository regression excluding external PDF baseline**

Run: `python -m pytest -q --ignore=tests/rag_ingestion/test_evaluation.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/context_eval tests/test_context_eval.py README.md
git commit -m "test: add context and recovery chaos evaluation"
```

### Task 9: Feature flag 默认值与生产验收

**Files:**
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_runtime_feature_flags.py`

**Interfaces:**
- Adds: Gateway、Prompt Registry、Context Planner、Recovery、Memory V2、Consolidation 和 shadow flags。
- Production default: new infrastructure enabled except consolidation initially disabled until first controlled run; shadow flag disabled after validation。

- [ ] **Step 1: Write failing flag tests**

```python
def test_production_defaults_enable_safe_runtime_layers():
    settings = Settings(_env_file=None)
    assert settings.model_gateway_enabled is True
    assert settings.prompt_registry_enabled is True
    assert settings.context_planner_enabled is True
    assert settings.recovery_orchestrator_enabled is True
    assert settings.memory_v2_enabled is True

def test_high_risk_cloud_egress_cannot_be_enabled_by_environment(monkeypatch):
    monkeypatch.setenv("HIGH_RISK_CLOUD_EGRESS_ALLOWED", "true")
    assert not hasattr(Settings(), "high_risk_cloud_egress_allowed")
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m pytest -q tests/test_runtime_feature_flags.py`

Expected: FAIL because flags are missing.

- [ ] **Step 3: Implement flags and environment documentation**

Expose only safe switches. There is no HIGH cloud override. Document rollback order and known baseline exception for local PDF gold data.

- [ ] **Step 4: Run final verification**

Run: `python -m pytest -q --ignore=tests/rag_ingestion/test_evaluation.py`

Run: `python -m app.harness.runner --json`

Run: `python -m app.context_eval.runner --json`

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py .env.example README.md tests/test_runtime_feature_flags.py
git commit -m "docs: enable production context and recovery runtime"
```

