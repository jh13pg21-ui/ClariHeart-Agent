# Task 2 迁移基线报告

## 状态

已完成。建立了 Alembic 基线与无损升级入口；生产 FastAPI 启动阶段不再调用 `Base.metadata.create_all()`，仅校验数据库版本。

## 改动文件

- `requirements.txt`：新增 `alembic==1.14.1`。
- `alembic.ini`、`migrations/**`：Alembic 配置、现有 Schema 基线 `0001` 与加法式认证/审计/发件箱迁移 `0002`。
- `app/cli/migrate.py`：提供 `python -m app.cli.migrate check|upgrade`，仅在所有旧表及列均匹配已知基线时自动 `stamp 0001`。
- `app/models/entities.py`：新增认证会话、安全审计、发件箱及已处理消息 ORM 映射，并补充用户认证迁移字段。
- `app/main.py`、`app/core/bootstrap.py`：启动只做 Alembic 版本检查；保留显式 `create_schema()` 供测试 Harness 使用。
- `tests/test_migrations.py`：覆盖空库、旧库升级、业务数据保留、未知结构拒绝、幂等及 ORM 对齐。

## RED 证据

在实现前运行指定命令：

```powershell
& 'D:\Docker\resources\bin\docker.exe' run --rm -v 'D:\AgentDevelop\mindbridge\.worktrees\production-hardening:/app' -w /app mindbridge-app python -m unittest tests.test_migrations -v
```

结果为 6 项中 5 项失败，失败根因明确为：`ModuleNotFoundError: No module named 'app.cli'`。未知/不完整结构拒绝用例因入口不存在而返回非零，按预期通过。

## GREEN 证据

现有 `mindbridge-app` 镜像未包含新增 Alembic 依赖，因此在一次性容器内临时安装 `alembic==1.14.1` 后验证（容器退出即删除，不写入业务数据）。

```text
python -m unittest tests.test_migrations -v  => Ran 6 tests ... OK
python -m unittest discover -s tests         => Ran 25 tests ... OK
git diff --check 3a3e283..HEAD                => exit 0（空白修复提交后复核）
```

完整测试输出仅含项目既有的 `datetime.utcnow` 与 FastAPI `on_event` 弃用警告，无测试失败。

## 无损迁移证据

- 对已知完整旧库，入口先执行 `stamp 0001_existing_schema_baseline`，再升级到 `0002_auth_audit_outbox_schema`。
- `0002` 只新增 `user_accounts` 的三列和四张新表；不删除、重建或更新任何已有业务表数据。
- 回归测试在升级前插入 `chat_messages.content='keep this message'` 和旧密码哈希，升级后逐值读取并确认不变。
- 对表集合或任一列集合不匹配的未版本化数据库，入口拒绝迁移并返回非零，且不创建 `alembic_version`。
- 二次 `upgrade` 测试确认版本表仅保留一行，证明升级幂等。

## 提交 SHA

实现提交：`f7c1139b684602bca5446c38deb686f9611e8e35`（`feat: add lossless alembic migration workflow`）。

## 关注点

- 将 `alembic` 纳入正式依赖后，部署镜像需要依据更新后的 `requirements.txt` 重建；旧镜像缺少该依赖，不能直接执行迁移 CLI。
- 未版本化数据库仅在完整匹配已知旧基线时自动升级；其他情况会安全拒绝，需人工核验并决定迁移路径。

## 修复轮次 1

- Dockerfile 现显式复制 `alembic.ini` 与 `migrations/`；新增测试覆盖该生产镜像产物契约。由于本机没有 `python:3.12-slim` 且 Docker Hub 授权请求返回 EOF，无法在本轮重建生产镜像；Dockerfile 内容测试已通过。
- SQLite 未版本化旧库现以实际执行 `0001` 生成的参考 Schema 进行完整 inspector 指纹比对，覆盖列类型、nullable、默认值、主键、外键、唯一约束和索引；同名同列但约束漂移的测试会被拒绝且不写版本表。
- 生产启动已删除 `seed_data()` 调用，仅检查 Alembic 版本；显式 Harness 初始化入口仍保留。
- 0002 已补齐新增表索引；为兼容 MySQL 8，移除了 TEXT 列的服务端默认值（这些值由 ORM 写入默认值处理）。
- 隔离 MySQL 8 验证：创建专用 Docker 网络与 `--tmpfs /var/lib/mysql` 的 `mindbridge-migration-test-mysql` 容器；首次 RED 暴露 MySQL 拒绝 TEXT 默认值。修复后空库 `upgrade` 与 `check` 均通过，随后删除容器和网络。未连接现有业务数据库。
