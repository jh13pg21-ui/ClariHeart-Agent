# MindBridge Production Hardening Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不丢失现有 MySQL 数据的前提下，将 MindBridge 升级为具备生产认证、真实输出安全审核、异步多 Agent、RabbitMQ 可靠任务和完整审计的系统。

**Architecture:** 保留 FastAPI 单体边界，按依赖顺序实施四个独立里程碑。MySQL 是业务权威数据源，Redis 负责记忆和限流，RabbitMQ/Celery 负责异步任务，Agent Runtime 使用阶段内并行和确定性 Artifact 合并。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、Argon2id、JWT、Redis、MySQL、RabbitMQ、Celery、httpx、Chroma、Ollama/OpenAI。

## Global Constraints

- 现有用户、会话、消息、心理报告、个案和知识块必须无损保留。
- 生产路径彻底移除 Basic Auth、默认账号和 SHA-256 新密码写入。
- 高风险报告和 Outbox 必须在学生回复前提交。
- HIGH 回复必须审核真实候选文本后一次性返回，失败使用安全模板。
- CHAT/LOW 保留 SSE；MEDIUM 只流式发送已审核文本。
- 管理员继续可查看完整会话，但访问必须审计。
- RabbitMQ 消息不得包含完整学生原文。
- 并行 Agent 不得共享同步 SQLAlchemy Session。
- 每个任务遵循先失败测试、最小实现、通过测试、独立提交。

---

## 执行顺序

1. [认证、数据库迁移与后台修复计划](2026-07-26-foundation-auth-migrations.md)
2. [异步多 Agent 与真实输出安全计划](2026-07-26-async-agent-output-safety.md)
3. [RabbitMQ、Celery 与 Transactional Outbox 计划](2026-07-26-rabbitmq-outbox-workers.md)
4. [输入安全、可观测性、CI 与发布加固计划](2026-07-26-hardening-observability-ci.md)

每个计划完成后运行该计划定义的验收命令并提交。第四个计划结束后运行：

```powershell
python -m unittest discover -s tests
docker compose config
docker compose build
docker compose run --rm app python -m alembic upgrade head
docker compose run --rm app python -m app.harness.runner --json
```

最终必须确认：

- 工作区无未提交业务改动。
- 基线提交 `90e354f` 可用于比较。
- 功能分支完整推送。
- 不推送 `.env`、GGUF、Chroma、Excel 和运行产物。

