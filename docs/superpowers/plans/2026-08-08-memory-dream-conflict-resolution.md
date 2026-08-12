# Memory Dream V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 MindBridge 增加写入时冲突仲裁、真实语义合并，以及带时间/扫描/会话/租约四层门控的自动 Dream 维护闭环。

**Architecture:** 使用 LongTermMemory V3 元数据表达稳定槽位、冲突组和合并血缘；使用每用户 MemoryDreamState 持久化扫描水位与可过期数据库租约。提取后和 Celery Beat 都只负责原子预留，真正整理经 Outbox/Celery 异步执行。

**Tech Stack:** Python 3.12、SQLAlchemy 2、Alembic、FastAPI、Celery、RabbitMQ、MySQL/SQLite、pytest。

## Global Constraints

- 在现有 `0808_production-context-memory-recovery` 分支原目录内实现，不创建额外 worktree。
- 不物理删除长期记忆历史。
- 高风险对话继续禁止长期记忆提取。
- 所有模型返回 ID 必须经过用户域白名单校验。
- 每项生产行为先写失败测试，再写最小实现。

---

### Task 1: Memory V3 schema and migration

**Files:**
- Modify: `app/models/entities.py`
- Create: `migrations/versions/0011_memory_dream_v3.py`
- Test: `tests/test_memory_dream_v3_migration.py`

**Interfaces:**
- Produces: `LongTermMemory.memory_key/conflict_group_id/consolidated_from_ids_json/resolution_reason`
- Produces: `MemoryDreamState` ORM model and `memory_dream_states` table

- [ ] Write migration/model tests for defaults, indexes, foreign keys and SQLite upgrade.
- [ ] Run the migration test and confirm it fails because V3 fields/table do not exist.
- [ ] Add ORM fields, new state model and portable Alembic upgrade/downgrade while preserving the frozen legacy schema.
- [ ] Run migration/model tests to green.

### Task 2: Candidate V3 and write-time conflict arbitration

**Files:**
- Modify: `app/schemas/dtos.py`
- Modify: `app/prompts/schemas/memory_candidate_v2.json`
- Modify: `app/prompts/tasks/memory_extraction.md`
- Modify: `app/services/long_term_memory.py`
- Test: `tests/test_long_term_memory_v3_conflicts.py`
- Modify: `tests/test_long_term_memory_v2.py`

**Interfaces:**
- Produces: backward-compatible `MemoryCandidate` fields `memory_key`, `action`, `related_memory_ids`, `reason`
- Produces: canonical key normalization and strict related-memory whitelist validation

- [ ] Write failing tests for exact confirm, same-slot supersede, explicit conflict isolation, related-ID rejection and preference reactivation.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement V3 parsing and deterministic arbitration while preserving V2 callers.
- [ ] Run focused tests to green and refactor normalization helpers.

### Task 3: Dream four-gate scheduler and expiring lease

**Files:**
- Modify: `app/core/config.py`
- Rewrite: `app/services/memory_consolidation.py`
- Test: `tests/test_memory_dream_gates.py`
- Modify: `tests/test_memory_consolidation.py`

**Interfaces:**
- Produces: `DreamGateDecision`
- Produces: `MemoryConsolidationService.reserve_schedule(user_id, trigger_reason, now)`
- Produces: stale lease recovery and run abandonment

- [ ] Write one failing test per time, scan, session and lease gate plus stale lease recovery.
- [ ] Run the gate suite and confirm failures are caused by missing Dream state behavior.
- [ ] Implement transactional state creation, scanning watermark, lease acquisition and recovery.
- [ ] Run gate suite to green.

### Task 4: Real consolidation mutations

**Files:**
- Create: `app/prompts/schemas/memory_consolidation_v2.json`
- Modify: `app/prompts/registry.py`
- Modify: `app/prompts/tasks/memory_consolidation.md`
- Modify: `app/services/memory_consolidation.py`
- Modify: `tests/test_memory_consolidation.py`

**Interfaces:**
- Consumes: Memory V3 fields and Dream lease
- Produces: strict KEEP/MERGE/SUPERSEDE/EXPIRE/CONFLICT decisions
- Produces: new merged LongTermMemory row with unioned evidence and lineage

- [ ] Write failing tests for merged-row creation, evidence union, explicit survivor, conflict quarantine and zero-mutation invalid output.
- [ ] Run tests and confirm red state.
- [ ] Implement strict parser and transactional mutation engine.
- [ ] Run tests to green.

### Task 5: Periodic scanning and reliable worker lifecycle

**Files:**
- Modify: `app/workers/tasks.py`
- Modify: `app/workers/celery_app.py`
- Modify: `app/workers/outbox_publisher.py`
- Modify: `tests/test_memory_consolidation_worker.py`
- Create: `tests/test_memory_dream_scheduler.py`

**Interfaces:**
- Produces: Celery task `scan_memory_dreams`
- Produces: Beat schedule and general-queue route
- Consumes: `reserve_schedule()` and existing transactional Outbox

- [ ] Write failing tests proving periodic scanning schedules without a new extraction and duplicate delivery is idempotent.
- [ ] Run focused tests to confirm red state.
- [ ] Implement batched user scan, Outbox enqueue, lifecycle finalization and Beat route.
- [ ] Run scheduler/worker tests to green.

### Task 6: Configuration, documentation and verification

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_migrations.py`

**Interfaces:**
- Documents: enabled-by-default Dream V3 controls, trigger sequence, conflict semantics and operational recovery

- [ ] Update environment defaults and README to match executable behavior.
- [ ] Run `git diff --check` and focused memory suites.
- [ ] Run complete pytest suite with the documented local PDF gold-data exclusion only if the file is absent.
- [ ] Run migration and engineering harness checks.
- [ ] Commit implementation in coherent commits and verify local branch state.
