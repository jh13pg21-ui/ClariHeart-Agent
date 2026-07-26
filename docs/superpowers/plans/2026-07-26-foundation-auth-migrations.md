# Foundation, Authentication and Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复后台报告接口，引入 Alembic 无损迁移，并用 Argon2id + JWT Cookie 会话替换 Basic Auth。

**Architecture:** 先恢复现有后台行为并建立回归测试，再增加数据库版本控制和认证表。认证逻辑集中在独立服务，FastAPI 依赖只消费认证结果，前端通过同源 HttpOnly Cookie 工作。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、argon2-cffi、PyJWT、MySQL、unittest。

## Global Constraints

- 不删除或重写现有业务数据。
- 旧密码不能继续作为生产密码使用。
- JWT 密钥必须来自环境变量。
- 管理员完整会话读取必须记录安全审计。

---

### Task 1: 修复 ReportService 并覆盖全部后台读取接口

**Files:**
- Modify: `app/services/report.py`
- Modify: `app/api/routes.py`
- Create: `tests/test_report_service.py`
- Create: `tests/test_admin_api.py`

**Interfaces:**
- Produces: `ReportService.latest_reports()`, `agent_run_traces()`, `tool_audits()`, `conversation()`
- Consumes: 现有 DTO 与 SQLAlchemy 实体

- [ ] **Step 1: 写失败测试**

```python
class ReportServiceShapeTests(unittest.TestCase):
    def test_expected_methods_are_bound_to_service(self):
        for name in ["latest_reports", "agent_run_traces", "tool_audits", "conversation"]:
            self.assertTrue(callable(getattr(ReportService, name, None)), name)
```

- [ ] **Step 2: 验证测试失败**

Run: `python -m unittest tests.test_report_service -v`  
Expected: `agent_run_traces`、`tool_audits` 或 `conversation` 缺失。

- [ ] **Step 3: 最小修复**

将错误缩进的方法恢复为 `ReportService` 实例方法，使 `_report_response()` 也位于类内。路由保持原 URL 和 DTO。

- [ ] **Step 4: 增加 API 回归测试**

使用临时 SQLite 和 TestClient 验证：

```python
self.assertEqual(client.get("/api/admin/reports", headers=admin_auth).status_code, 200)
self.assertEqual(client.get("/api/admin/agent-traces", headers=admin_auth).status_code, 200)
self.assertEqual(client.get("/api/admin/tool-audits", headers=admin_auth).status_code, 200)
```

- [ ] **Step 5: 运行测试并提交**

Run: `python -m unittest tests.test_report_service tests.test_admin_api -v`  
Expected: PASS

Commit: `fix: restore report and audit service endpoints`

### Task 2: 建立 Alembic 基线与无损升级入口

**Files:**
- Modify: `requirements.txt`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_existing_schema_baseline.py`
- Create: `migrations/versions/0002_auth_audit_outbox_schema.py`
- Create: `app/cli/migrate.py`
- Modify: `app/main.py`
- Modify: `app/core/bootstrap.py`
- Create: `tests/test_migrations.py`

**Interfaces:**
- Produces: `python -m app.cli.migrate check|upgrade`
- Produces: Alembic `head`
- Consumes: `app.core.database.Base.metadata`

- [ ] **Step 1: 写新库和现有库失败测试**

```python
def test_upgrade_creates_schema_on_empty_database(self):
    run_upgrade(self.empty_url)
    self.assertIn("user_accounts", inspect(self.engine).get_table_names())

def test_upgrade_preserves_existing_rows(self):
    before = count_rows(self.legacy_engine, "chat_messages")
    run_upgrade(self.legacy_url)
    self.assertEqual(count_rows(self.legacy_engine, "chat_messages"), before)
```

- [ ] **Step 2: 运行并确认缺少迁移入口**

Run: `python -m unittest tests.test_migrations -v`  
Expected: FAIL，无法导入迁移 CLI 或找不到 Alembic 配置。

- [ ] **Step 3: 创建迁移结构**

`0001` 描述当前基线 Schema；`0002` 只做加法：

```python
op.add_column("user_accounts", sa.Column("password_algorithm", sa.String(32), nullable=False, server_default="legacy_sha256"))
op.add_column("user_accounts", sa.Column("must_reset_password", sa.Boolean(), nullable=False, server_default=sa.true()))
op.add_column("user_accounts", sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()))
```

并创建 `auth_sessions`、`security_audit_records`、`outbox_events`、`processed_messages`。

- [ ] **Step 4: 实现安全基线识别**

`app.cli.migrate check` 必须验证已知核心表和关键列。已有表且无 `alembic_version` 时只允许对匹配的基线执行 `stamp 0001`，随后升级 `head`；结构不匹配返回非零退出码。

- [ ] **Step 5: 移除生产启动 create_all**

FastAPI 启动仅检查当前数据库版本；测试 Harness 可显式创建临时 Schema。

- [ ] **Step 6: 验证并提交**

Run: `python -m unittest tests.test_migrations -v`  
Expected: 新库建库通过、旧库记录数不变、未知结构被拒绝。

Commit: `feat: add lossless alembic migration workflow`

### Task 3: 实现 Argon2id、JWT 与可轮换会话

**Files:**
- Modify: `requirements.txt`
- Modify: `app/core/config.py`
- Modify: `app/models/entities.py`
- Create: `app/services/auth.py`
- Replace: `app/core/security.py`
- Create: `tests/test_auth_service.py`

**Interfaces:**
- Produces: `AuthService.login(username: str, password: str) -> AuthTokens`
- Produces: `AuthService.refresh(refresh_token: str) -> AuthTokens`
- Produces: `AuthService.logout(session_id: str) -> None`
- Produces: `authenticate_request(request, db) -> UserAccount`

- [ ] **Step 1: 写密码和 Token 失败测试**

```python
def test_password_hash_is_argon2id(self):
    encoded = password_hasher.hash("A-strong-password-2026")
    self.assertTrue(encoded.startswith("$argon2id$"))

def test_refresh_rotates_token(self):
    first = service.login("student", "A-strong-password-2026")
    second = service.refresh(first.refresh_token)
    self.assertNotEqual(first.refresh_token, second.refresh_token)
    with self.assertRaises(InvalidRefreshToken):
        service.refresh(first.refresh_token)
```

- [ ] **Step 2: 验证失败**

Run: `python -m unittest tests.test_auth_service -v`  
Expected: FAIL，认证服务不存在。

- [ ] **Step 3: 增加配置**

```python
jwt_secret_key: str
jwt_algorithm: str = "HS256"
access_token_minutes: int = 15
refresh_token_days: int = 7
auth_secure_cookie: bool = True
auth_cookie_samesite: str = "lax"
```

空 `JWT_SECRET_KEY` 在非测试环境启动时必须报错。

- [ ] **Step 4: 实现认证服务**

Refresh Token 使用 256 位随机值；数据库只保存 `sha256(refresh_token)`。旋转操作在单一事务中撤销旧 Session 并创建新 Session。

- [ ] **Step 5: 替换安全依赖**

删除 Basic Header 解析。`current_user` 从 Access Cookie 验证 JWT、用户禁用状态和 Session 撤销状态。

- [ ] **Step 6: 验证并提交**

Run: `python -m unittest tests.test_auth_service -v`  
Expected: PASS

Commit: `feat: replace basic auth with rotating jwt sessions`

### Task 4: 接入 Auth API、CSRF、前端和安全 CLI

**Files:**
- Create: `app/api/auth_routes.py`
- Modify: `app/api/routes.py`
- Modify: `app/main.py`
- Create: `app/services/security_audit.py`
- Create: `app/cli/users.py`
- Modify: `app/static/app.js`
- Modify: `app/static/student.js`
- Modify: `app/static/admin.js`
- Modify: `app/static/index.html`
- Create: `tests/test_auth_api.py`
- Modify: `tests/test_admin_api.py`

**Interfaces:**
- Produces: `POST /api/auth/login|refresh|logout`
- Produces: `SecurityAuditService.record(...)`
- Consumes: Task 3 `AuthService`

- [ ] **Step 1: 写 API 失败测试**

```python
response = client.post("/api/auth/login", json={"username": "student", "password": password})
self.assertEqual(response.status_code, 200)
self.assertIn("mindbridge_access", response.cookies)
self.assertEqual(client.get("/api/profile").status_code, 200)
```

同时验证缺少 CSRF Header 的写请求返回 403。

- [ ] **Step 2: 实现 Cookie 与 CSRF**

登录设置 Access、Refresh 和可读 CSRF Cookie。状态修改请求要求 `X-CSRF-Token` 等于 CSRF Cookie。

- [ ] **Step 3: 更新前端**

所有 `fetch` 使用：

```javascript
fetch(path, {
  ...options,
  credentials: "same-origin",
  headers: { ...headers, "X-CSRF-Token": readCsrfCookie() }
})
```

删除 Basic Token 和 `sessionStorage` 认证逻辑。

- [ ] **Step 4: 增加用户 CLI**

CLI 使用 `getpass.getpass()` 读取密码，支持 create、set-password、disable；禁止命令行明文密码参数。

- [ ] **Step 5: 会话读取审计**

`/api/admin/conversations/{session_id}` 成功或失败均写入 `security_audit_records`。

- [ ] **Step 6: 验证并提交**

Run: `python -m unittest tests.test_auth_api tests.test_admin_api -v`  
Expected: PASS

Commit: `feat: add secure auth api csrf and access auditing`

### Milestone Verification

Run:

```powershell
python -m unittest tests.test_report_service tests.test_admin_api tests.test_migrations tests.test_auth_service tests.test_auth_api -v
python -m unittest discover -s tests
```

Expected: 全部 PASS，旧 Basic Auth 测试已迁移为 JWT 测试。

