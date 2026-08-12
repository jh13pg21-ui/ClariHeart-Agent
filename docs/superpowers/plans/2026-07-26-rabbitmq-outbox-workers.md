# RabbitMQ, Celery and Transactional Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 RabbitMQ/Celery 和 Transactional Outbox 替换进程内工具线程池，保证高风险任务可靠、幂等、可审计。

**Architecture:** Harness 在业务事务内写入报告和 Outbox；独立 Publisher 使用 Confirm 投递 RabbitMQ；独立 Celery Worker 在治理授权后执行工具，使用 MySQL 幂等表防止重复副作用。

**Tech Stack:** RabbitMQ、Celery、SQLAlchemy、MySQL、Docker Compose、unittest。

## Global Constraints

- RabbitMQ 消息只携带业务 ID。
- 高风险 Outbox 在回复发送前提交。
- 重复发布和重复消费不得重复创建个案或预警。
- FastAPI 不再启动进程内 ToolQueueWorker。

---

### Task 1: Outbox 实体与事务服务

**Files:**
- Modify: `app/models/entities.py`
- Create: `app/services/outbox.py`
- Modify: `app/agents/harness.py`
- Create: `tests/test_outbox.py`

**Interfaces:**
- Produces: `OutboxService.add_event(db, event_type, aggregate_type, aggregate_id, payload, idempotency_key)`
- Produces: `OutboxEvent`

- [ ] **Step 1: 写原子性失败测试**

```python
with self.assertRaises(RuntimeError):
    with db.begin():
        create_report(db)
        OutboxService.add_event(...)
        raise RuntimeError("rollback")
self.assertEqual(db.query(OutboxEvent).count(), 0)
```

- [ ] **Step 2: 实现 Outbox**

使用唯一 `event_id` 和 `idempotency_key`；Payload 只包含 `reportId`、`riskLevel`、`eventId`。

- [ ] **Step 3: Harness 同事务写入**

CONSULT/LOW 写 `report.excel`；MEDIUM 增加 `case.create`；HIGH 写高风险个案请求。用户消息、报告、Trace 初始记录和 Outbox 使用同一事务。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_outbox -v`  
Expected: PASS

Commit: `feat: persist report events with transactional outbox`

### Task 2: RabbitMQ Publisher Confirm

**Files:**
- Modify: `requirements.txt`
- Create: `app/workers/__init__.py`
- Create: `app/workers/outbox_publisher.py`
- Modify: `app/core/config.py`
- Create: `tests/test_outbox_publisher.py`

**Interfaces:**
- Produces: `OutboxPublisher.publish_batch(limit: int) -> int`
- Consumes: PENDING OutboxEvent

- [ ] **Step 1: 写成功和失败状态测试**

Mock Broker Confirm：

```python
published = publisher.publish_batch(10)
self.assertEqual(published, 1)
self.assertIsNotNone(event.published_at)
```

Broker 异常时断言事件仍为 PENDING、attempts 增加且设置 `available_at`。

- [ ] **Step 2: 实现行锁抢占**

MySQL 使用 `with_for_update(skip_locked=True)`；SQLite 测试使用单实例兼容路径。

- [ ] **Step 3: 实现 Confirm 和退避**

使用持久化交换机、持久化消息、Publisher Confirm。退避为 `min(300, 2 ** attempts)` 秒。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_outbox_publisher -v`  
Expected: PASS

Commit: `feat: publish outbox events with rabbitmq confirms`

### Task 3: Celery Worker、治理和幂等

**Files:**
- Create: `app/workers/celery_app.py`
- Create: `app/workers/tasks.py`
- Modify: `app/services/tool_governance.py`
- Modify: `app/services/tools.py`
- Modify: `app/models/entities.py`
- Create: `tests/test_worker_tasks.py`

**Interfaces:**
- Produces: `process_excel(event_id, report_id)`
- Produces: `create_case(event_id, report_id)`
- Produces: `send_high_risk_alert(event_id, case_id)`

- [ ] **Step 1: 写未授权与重复消费测试**

```python
first = run_task(event_id)
second = run_task(event_id)
self.assertEqual(db.query(ExcelRecord).count(), 1)
self.assertEqual(second.status, "ALREADY_PROCESSED")
```

LOW 风险调用高风险预警必须写 BLOCKED 审计且不发送。

- [ ] **Step 2: 接入 ToolGovernance**

每个任务执行 `start_job`、`require_allowed`、`finish`。异常分类为可重试和永久失败。

- [ ] **Step 3: 实现幂等事务**

成功副作用和 `processed_messages` 尽可能在同一事务完成；Excel/SMTP 这类外部副作用使用业务唯一键和已有成功记录二次防护。

- [ ] **Step 4: 实现事件编排**

`case.create` 成功后发送 `case.created`；HIGH Alert 只消费 `case.created`，不轮询 ToolJob 依赖。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest tests.test_worker_tasks tests.test_tool_governance -v`  
Expected: PASS

Commit: `feat: execute governed idempotent celery tasks`

### Task 4: 移除 Web 进程 Worker 并增加 Docker 服务

**Files:**
- Modify: `app/main.py`
- Modify: `app/services/chat.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `Dockerfile`
- Create: `tests/test_no_inprocess_worker.py`

**Interfaces:**
- Produces: Compose 服务 `rabbitmq`, `worker-general`, `worker-alert`, `outbox-publisher`

- [ ] **Step 1: 写 FastAPI 不启动线程 Worker 的失败测试**

```python
app = create_app()
self.assertFalse(any(handler.__name__ == "start_tool_worker" for handler in app.router.on_startup))
```

- [ ] **Step 2: 移除进程内 ToolQueueWorker**

Chat 请求不直接调用 MCP 或线程池，只写 Outbox。保留旧 ToolJob 数据读取兼容。

- [ ] **Step 3: 增加 Compose**

RabbitMQ 启用健康检查和持久化 Volume。两个 Worker 使用不同队列；Publisher 等待 MySQL 和 RabbitMQ 健康。

- [ ] **Step 4: 增加容器测试阶段**

Dockerfile 使用 base/test/production 多阶段，生产阶段不复制 `tests`。

- [ ] **Step 5: 验证并提交**

Run:

```powershell
python -m unittest tests.test_no_inprocess_worker -v
docker compose config
```

Expected: PASS，Compose 配置有效。

Commit: `build: add rabbitmq celery workers and outbox publisher`

### Milestone Verification

Run:

```powershell
python -m unittest tests.test_outbox tests.test_outbox_publisher tests.test_worker_tasks tests.test_no_inprocess_worker -v
docker compose up -d rabbitmq mysql redis
docker compose run --rm worker-general celery -A app.workers.celery_app inspect ping
```

Expected: 单元测试 PASS，RabbitMQ 健康，Celery Worker 可连接 Broker。

