# Foundation Task 3–4 认证与审计报告

## 状态

DONE。已用 Argon2id、JWT HttpOnly Cookie、可轮换 Refresh Session 和双提交 CSRF 完整替换应用 Basic Auth，并同步迁移 Auth API、管理员访问审计、安全用户 CLI 与三份前端脚本。

## RED 证据

首个密码行为测试在既有 SHA256 实现上得到明确失败：

```text
python -m unittest tests.test_auth_service -v
=> Ran 1 test
=> FAILED (failures=1)
=> encoded.startswith("$argon2id$") 为 False
```

扩展认证纵切契约后，修复前结果明确显示缺少认证服务和安全 CLI：

```text
python -m unittest tests.test_auth_service tests.test_auth_api -v
=> ModuleNotFoundError: No module named 'app.services.auth'
=> ModuleNotFoundError: No module named 'app.cli.users'
```

以上命令均在任务指定的 `mindbridge-app` 隔离容器内运行；容器中一次性安装 `argon2-cffi`、`PyJWT` 与 `alembic==1.14.1`，未连接或改写业务数据库。

## GREEN 证据

目标测试：

```text
python -m unittest tests.test_auth_service tests.test_auth_api tests.test_admin_api -v
=> Ran 15 tests in 1.609s
=> OK
```

完整回归：

```text
python -m unittest discover -s tests -v
=> Ran 59 tests in 23.204s
=> OK (skipped=7)
```

7 项 Skip 均为需要隔离 MySQL URL 与显式 destructive test 双重确认的既有迁移测试；其余测试无失败。`node --check` 已验证 `app.js`、`student.js`、`admin.js` 语法，`git diff --check` 返回 0。

## 安全约束

- 密码仅以 `$argon2id$` 编码存储和验证；`legacy_sha256` 或 `must_reset_password` 账号在比较旧哈希前即进入 `PasswordResetRequired`。
- 非测试环境启动前强制检查来自环境变量的 JWT 密钥，缺失或少于 32 字节时拒绝启动；算法固定为 HS256。
- Access JWT 含 `sub`、`sid`、`roles`、`iat`、`exp`、`jti`；每次请求同时核验数据库 Session、用户禁用状态与角色声明。
- Refresh Token 使用 256 位 CSPRNG 值，数据库仅保存 SHA256 摘要；轮换使用行锁并在同一事务中撤销旧 Session、创建新 Session，旧 Token 无法重放。
- Access/Refresh Cookie 为 HttpOnly；CSRF Cookie 可读。除登录外，所有状态修改请求均常量时间比较 Cookie 与 `X-CSRF-Token`。
- Basic Header 解析、前端 Basic/Authorization 和 `sessionStorage` 凭据逻辑已删除；所有前端 `fetch` 使用 `credentials: "same-origin"`，写请求自动附带 CSRF。
- 管理员完整会话读取的 success、not_found、error 均写安全审计，只记录 actor、action、resource、outcome、IP 和时间，不写聊天内容、密码或 Token。
- 用户 CLI 仅通过 `getpass` 交互读取密码，命令行解析器不接受明文密码参数。
- 未修改 0001/0002 迁移，也未删除、重写或连接现有业务数据。

## 提交 SHA

实现提交为本报告所在的 `feat: add secure jwt authentication and auditing` 提交；最终 SHA 以交付时 `git rev-parse HEAD` 输出为准。

## 未解决问题

- 无认证功能或安全约束缺口。
- 测试输出仍含项目既有的 `datetime.utcnow()`、FastAPI `on_event` 弃用告警，以及一次 AnyIO ResourceWarning；不影响测试结果，本任务未扩大范围处理。
