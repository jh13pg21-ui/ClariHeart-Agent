# Hardening, Observability and CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成输入安全、隐私、可观测性、CI、部署文档和全链路验收。

**Architecture:** 在既有服务边界内增加配置驱动的验证、限流和结构化 Trace；CI 分别验证快速单元测试、MySQL 迁移和 RabbitMQ/Celery 集成。

**Tech Stack:** FastAPI、Redis、SQLAlchemy、GitHub Actions、Docker Compose、unittest。

## Global Constraints

- 不改变管理员可查看完整会话的决定。
- 日志、RabbitMQ 和普通 Trace 不记录完整敏感正文或密钥。
- 限制值必须可配置并有安全默认值。

---

### Task 1: 输入、文件和限流

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/schemas/dtos.py`
- Modify: `app/api/routes.py`
- Modify: `app/services/knowledge.py`
- Create: `app/services/rate_limit.py`
- Create: `tests/test_input_security.py`

**Interfaces:**
- Produces: `RateLimiter.check(scope: str, subject: str) -> None`

- [ ] **Step 1: 写边界失败测试**

验证：

- 4001 字符 Chat 返回 422。
- 非法 Session ID 返回 422。
- 10 MiB 以上文件返回 413。
- 非 md/txt/pdf 返回 415。
- PDF 页数或提取文本超限返回 413。

- [ ] **Step 2: 增加配置**

```python
chat_message_max_chars: int = 4000
knowledge_upload_max_bytes: int = 10 * 1024 * 1024
knowledge_pdf_max_pages: int = 200
knowledge_text_max_chars: int = 1_000_000
```

- [ ] **Step 3: 实现 Redis 滑动窗口限流**

登录、刷新、聊天和管理写操作使用独立 Scope。Redis 不可用时登录和高风险写操作采用进程内保守限流，普通读取不阻断。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_input_security -v`  
Expected: PASS

Commit: `feat: enforce request upload and rate limits`

### Task 2: 扩展脱敏与安全日志

**Files:**
- Modify: `app/services/privacy.py`
- Create: `app/core/logging.py`
- Modify: `app/services/trace.py`
- Create: `tests/test_privacy_logging.py`

**Interfaces:**
- Produces: `PrivacySanitizer.sanitize(text) -> str`
- Produces: `configure_logging(settings) -> None`

- [ ] **Step 1: 写脱敏失败测试**

覆盖手机号、邮箱、身份证、银行卡、IPv4、QQ/微信号和学号格式。断言 JWT、Cookie、SMTP 和 RabbitMQ URL 字段在日志结构中显示 `[REDACTED]`。

- [ ] **Step 2: 实现字段级日志过滤**

过滤键名：

```text
authorization, cookie, access_token, refresh_token, password,
smtp_password, rabbitmq_url, openai_api_key
```

- [ ] **Step 3: 精简 Trace**

只保存一次原始 Collaboration Event；AgentStep 保存阶段摘要，不重复事件。Prompt 只保存摘要哈希和模型元数据。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_privacy_logging -v`  
Expected: PASS

Commit: `feat: strengthen privacy redaction and safe tracing`

### Task 3: 请求、Agent、Outbox 和工具关联观测

**Files:**
- Create: `app/core/request_context.py`
- Modify: `app/main.py`
- Modify: `app/services/trace.py`
- Modify: `app/services/outbox.py`
- Modify: `app/services/tool_governance.py`
- Modify: `app/schemas/dtos.py`
- Create: `tests/test_observability.py`

**Interfaces:**
- Produces: `request_id` Middleware
- Produces: Trace `stageTimings`, `provider`, `model`, `degradationReasons`

- [ ] **Step 1: 写关联 ID 失败测试**

同一高风险请求产生的 Agent Trace、Outbox Event 和 Tool Audit 必须可通过 `request_id/turn_id/event_id` 追踪。

- [ ] **Step 2: 实现请求上下文**

接收合法 `X-Request-ID` 或生成 UUID，响应回写 Header；使用 `contextvars` 传递。

- [ ] **Step 3: 记录阶段耗时和降级**

记录 Intent、Risk、Memory、RAG、Response、SafetyReview 的毫秒耗时，以及 Redis、向量或模型降级原因。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_observability -v`  
Expected: PASS

Commit: `feat: correlate requests agent runs and tool events`

### Task 4: CI、Harness 和部署文档

**Files:**
- Modify: `.github/workflows/test.yml`
- Modify: `app/harness/runner.py`
- Modify: `README.md`
- Modify: `本地启动说明.md`
- Create: `docs/production-deployment.md`
- Create: `tests/test_harness_contract.py`

**Interfaces:**
- Produces: GitHub Actions 单元、MySQL 迁移、RabbitMQ/Celery 集成 Jobs

- [ ] **Step 1: 扩展 Harness 合同测试**

Harness 必须输出认证、安全输出、Outbox、Worker 幂等、迁移和并行耗时结果，且 JSON `passed` 为 true。

- [ ] **Step 2: 更新 CI**

GitHub Actions Services 使用 MySQL、Redis、RabbitMQ；按顺序运行 compileall、unit、migration、integration、harness。

- [ ] **Step 3: 更新文档**

文档明确：

- 生成 JWT 密钥。
- 迁移预检查与升级。
- 管理员创建和密码设置。
- RabbitMQ/Worker 启动。
- 高风险烟雾测试。
- 回滚顺序。
- GGUF 继续独立分发。

- [ ] **Step 4: 验证并提交**

Run:

```powershell
python -m unittest tests.test_harness_contract -v
python -m compileall app tests
```

Expected: PASS

Commit: `docs: add production deployment and ci verification`

### Task 5: 全量回归与发布前检查

**Files:**
- Modify only files required by failing regressions

**Interfaces:**
- Consumes: 前三份计划全部产物

- [ ] **Step 1: 运行全量单元测试**

Run: `python -m unittest discover -s tests`  
Expected: 0 failures, 0 errors。

- [ ] **Step 2: 运行迁移验证**

Run:

```powershell
docker compose run --rm app python -m app.cli.migrate check
docker compose run --rm app python -m app.cli.migrate upgrade
```

Expected: 当前 Schema 可识别，升级到 head，记录数不下降。

- [ ] **Step 3: 运行服务集成验证**

Run:

```powershell
docker compose up -d --build
docker compose ps
docker compose exec -T app python -m app.harness.runner --json
```

Expected: 服务健康，Harness `passed: true`。

- [ ] **Step 4: 验证关键故障场景**

- 停止 RabbitMQ，产生 HIGH 报告，确认 Outbox 为 PENDING。
- 恢复 RabbitMQ，确认事件发布和 Worker 成功。
- 重放同一消息，确认无重复个案和邮件。
- 中断 HIGH 客户端连接，确认预警仍成功。
- 停止 Ollama，确认 HIGH 返回安全模板。

- [ ] **Step 5: 检查仓库并提交**

Run:

```powershell
git diff --check
git status --short
git ls-files .env
git ls-files "*.gguf"
```

Expected: 无格式错误；`.env` 与 GGUF 不在 Git 索引。

Commit: `chore: complete production hardening verification`

