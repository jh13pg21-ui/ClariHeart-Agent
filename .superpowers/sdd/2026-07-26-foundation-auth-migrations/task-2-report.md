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

## 修复轮次 4：MySQL 精确旧基线误拒绝

### 状态与隔离环境

已完成。只连接任务专用的 `mysql:8.0` 容器 `mindbridge-t2-r4-mysql`，容器使用专网 `mindbridge-t2-r4-net`、`--tmpfs /var/lib/mysql`、预创建数据库和无 `CREATE DATABASE` 需求的测试用户。验证结束后按精确名称校验并删除容器与网络，复核均无残留；未连接或修改任何现有 MindBridge 数据库。

### Phase 1：稳定 RED 与结构化差异

精确执行 `0001_existing_schema_baseline`、删除 `alembic_version` 后运行：

```powershell
docker run --rm --network mindbridge-t2-r4-net -v <worktree>:/app -w /app `
  -e MINDBRIDGE_TEST_MYSQL_URL=<隔离数据库 URL> mindbridge-app `
  sh -lc "python -m pip install --no-cache-dir alembic==1.14.1 &&
          python -m unittest tests.test_migrations_mysql.MySqlMigrationWorkflowTests.test_exact_legacy_mysql_is_stamped_and_preserves_rows -v"
```

结果：`Ran 1 test`，`FAILED (failures=1)`；CLI 返回
`unversioned database does not match the known legacy baseline`。

随后诊断按表、列、type/default/null/PK、FK、UK、index 输出期望与 Inspector 实际值，共得到 7 处差异：

- `agent_run_traces`、`chat_messages`、`psychological_reports`：期望无显式索引，MySQL 实际反射外键列的服务端支撑索引。
- `chat_sessions`：实际反射 `user_id` 外键支撑索引，并将 `public_id` 唯一约束同时暴露为 `duplicates_index` 唯一索引。
- `risk_cases`、`user_accounts`：唯一约束同时反射为唯一索引。
- `tool_audit_records.allowed`：声明态为 `BOOL DEFAULT true`，执行后反射为 `TINYINT(1) DEFAULT '1'`。
- 表、列顺序、nullable、PK、FK 和 UK 本身无差异。

唯一根因是旧算法直接比较 0001 的 SQLAlchemy 声明态 `MetaData` 与 MySQL 执行后的 Inspector 规范态；MySQL 的等价类型规范化和服务端隐式索引使两个不同语义层必然不相等，并非真实 Schema 漂移。

### Phase 2/3：方言表示与最小假设

原始 Inspector 结果确认唯一约束带 `duplicates_index`，外键支撑索引没有对应的 0001 显式 `Index`，`Boolean()` 经 MySQL DDL/反射成为 `TINYINT(display_width=1)`。

第一个最小假设“直接使用 Alembic MySQL `compare_metadata`”被否证：类型、FK/UK 和隐式索引均能正确比较，但精确基线仍对空字符串默认值及 `true/1` 报两个 `modify_default` 假阳性。

第二个单一假设为：由 Alembic 方言比较器处理 type/null/FK/UK/index，默认值使用严格、类型感知的规范化，仅承认 MySQL Boolean 的 `true↔1`、`false↔0` 等价，同时显式保留列顺序与 PK 比较。最小诊断结果：

```text
exact baseline => []
VARCHAR(63) drift => modify_type
dropped foreign key => add_fk
unexpected index => remove_index
```

该比较只读取目标数据库并在内存中构造 0001 `MetaData`，不会在生产数据库创建临时表、临时 Schema 或比较数据库，也不要求 `CREATE DATABASE` 权限。

### Phase 4：TDD 最小修复

扩展 `tests/test_migrations_mysql.py` 后先运行修复前 RED：

```powershell
python -m unittest tests.test_migrations_mysql -v
```

结果：`Ran 5 tests`，`FAILED (failures=2)`；精确基线自动升级和其后的幂等升级失败，类型长度、FK、索引漂移的安全拒绝均通过。

最小修复仅修改 `app/cli/migrate.py`：从 0001 捕获内存 `MetaData`；非 SQLite 数据库先严格比较列顺序与 PK，再使用 Alembic 方言感知比较器及严格默认值回调判断是否匹配。SQLite 的同方言参考数据库指纹路径保持不变。

### GREEN 与无损证据

```text
python -m unittest tests.test_migrations_mysql -v
=> Ran 5 tests in 6.396s, OK

python -m unittest tests.test_migrations -v
=> Ran 9 tests in 11.728s, OK

MINDBRIDGE_TEST_MYSQL_URL=<隔离数据库 URL> python -m unittest discover -s tests
=> Ran 33 tests in 17.617s, OK

git diff --check
=> exit 0
```

MySQL 自动化用例确认：

- 精确由 0001 创建的未版本化旧库会自动 stamp 并升级到 `0002_auth_audit_outbox_schema`。
- `user_accounts.password_hash='legacy-hash'` 与 `chat_messages.content='preserve-me'` 升级后逐值不变。
- 二次升级返回零，`alembic_version` 仅一行且仍为 head。
- `VARCHAR(63)` 长度漂移、删除外键、增加非基线索引均返回非零，且不创建 `alembic_version`。

全量测试仅出现项目既有的 `datetime.utcnow` 与 FastAPI `on_event` 弃用警告，无失败。轮次 4 的 follow-up 提交 SHA 以本节所在提交及最终交付 HEAD 为准。

## 修复轮次 5：冻结历史 ORM 基线与 MySQL 防误删护栏

### 状态

DONE。本轮以 `3a3e283` 的历史 `app/models/entities.py` 为事实源完成独立审计，不再用 `0001` 生成参考结构后自证。修复轮次 5 的实现提交为
`aedc4c7b`（`fix: freeze historical migration schema`）。

截至本轮交付，复审指出的历史索引/default 不等价、0002 default 语义不一致和 MySQL 测试误删风险均已闭环；独立终审发现的 SQLite 索引名及 FK options 指纹遗漏也已通过 RED/GREEN 补齐，无未解决的 Critical/Important。

### 历史 ORM 与 0001 等价证据

- 新增 `migrations/legacy_schema.py` 作为单一权威 `MetaData`；生产代码和测试运行时均不依赖 git。
- 人工冻结并由契约测试逐项验证历史 13 张表、全部列顺序/类型/nullable、13 个 `id` 主键、7 个 FK、26 个固定命名索引及 35 个 Python client defaults。
- 历史 ORM 的 server defaults 为 0 个。旧 `0001` 错误增加的 7 个 server defaults 已全部移除：
  `roles_csv`、`owner`、`attempts`、`max_attempts`、`risk_level`、`policy`、`allowed`。
- `username`、`public_id`、`risk_cases.report_id` 保持为历史 ORM 的具名唯一索引，而不是额外 `UniqueConstraint`。
- `0001_existing_schema_baseline` revision id 保持不变，并直接从冻结 `MetaData` 创建结构；CLI 同样直接从该对象比较，不再动态加载或执行 `0001`。

初始契约 RED：

```text
python -m unittest tests.test_legacy_schema_contract -v
=> Ran 1 test, FAILED (failures=1)
=> migrations.legacy_schema 不存在
```

冻结 Schema 接入旧库升级路径后的 RED：

```text
python -m unittest tests.test_migrations -v
=> Ran 12 tests, FAILED (failures=4)
```

四项失败分别证明旧 `0001` 不等价、冻结历史 SQLite 未版本库被误拒、旧数据升级被误拒，以及 head 与当前 ORM 不一致。

独立终审还发现并复现两个 SQLite 指纹缺口：

```text
同列/同 unique 但错误索引名 => CLI returncode 0（错误 stamp）
同目标 FK 增加 ON DELETE CASCADE => CLI returncode 0（错误 stamp）
```

签名纳入索引/唯一约束名称及规范化 FK options 后，两项均被拒绝且不创建 `alembic_version`。

### 0002 与当前 ORM 对齐

- 移除 `security_audit_records.resource_id/ip_address` 及
  `outbox_events.status/attempts` 的错误 server defaults；这些仍由 ORM client defaults 处理。
- 保留 ORM 与迁移双方确实声明的 server defaults：
  `password_algorithm`、`must_reset_password`、`disabled`、`auth_sessions.revoked`。
- MySQL Boolean 的 `true/false` 与 `1/0` 继续按方言等价比较。
- 所有 TEXT 列均无 MySQL 8 不兼容的 server default。
- SQLite head 与当前 `Base.metadata.create_all()` 的所有表、列、类型、nullable、PK、FK、UK、索引名称/唯一性和 server defaults 物理签名一致。

### MySQL destructive 测试护栏

`tests/test_migrations_mysql.py` 在任何 `SET FOREIGN_KEY_CHECKS` 或 `DROP TABLE` 前执行：

1. 要求 `MINDBRIDGE_TEST_MYSQL_URL`；
2. 要求 `MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS=1`；
3. 使用 SQLAlchemy 解析 URL 中的数据库名；
4. 连接后查询 `SELECT DATABASE()`；
5. 要求 URL 与实际库名完全一致，且库名以 `mindbridge_test_` 开头。

生产样式库名、缺少确认以及 URL/实际库不一致的单测均会在 transaction/DDL 前失败。安全契约 RED 为 3 项失败；最小实现后：

```text
python -m unittest tests.test_mysql_test_safety -v
=> Ran 5 tests, OK
```

无 MySQL URL 或无双确认时，7 项 destructive 集成测试全部 Skip，不会创建 engine 或执行 DDL。

### 隔离 MySQL 8 与无损升级证据

只使用本轮一次性环境：

- 容器：`mindbridge-t2-fix5-mysql`，镜像 `mysql:8.0`，无宿主端口；
- 专网：`mindbridge-t2-fix5-net`；
- 数据目录：`/var/lib/mysql` tmpfs；
- 数据库：`mindbridge_test_migrations`；
- 用户：`mindbridge_test_runner`。

`SHOW GRANTS` 仅显示全局 `USAGE ON *.*` 和目标库级
`ALL PRIVILEGES ON mindbridge_test_migrations.*`，没有全局 CREATE DATABASE 权限。

```text
python -m unittest tests.test_migrations_mysql -v
=> Ran 7 tests in 8.631s, OK
```

MySQL 自动化确认：

- 直接由冻结历史 ORM 等价 `MetaData.create_all()` 创建的未版本库可自动 stamp 并升级到 head；
- `user_accounts.password_hash='legacy-hash'` 与
  `chat_messages.content='preserve-me'` 升级后逐值不变；
- `VARCHAR(63)`、删除 FK、额外索引及错误 server default 漂移均被拒绝，且不创建 `alembic_version`；
- head 与当前 ORM 的列顺序、PK、type/FK/UK/index/default 无 autogenerate 差异；
- 二次升级幂等，版本表仅一行。

验证后按精确 ID/名称删除容器和网络，并用精确名称过滤复查为空；未连接或修改任何既有 MindBridge 数据库。

### 最终验证

```text
python -m unittest tests.test_legacy_schema_contract tests.test_migrations tests.test_mysql_test_safety -v
=> Ran 19 tests, OK

MINDBRIDGE_TEST_MYSQL_URL=<隔离 URL>
MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS=1
python -m unittest discover -s tests -v
=> Ran 44 tests in 28.817s, OK

python -m app.cli.migrate check
alembic.command.check(...)
=> No new upgrade operations detected.

git diff --check
=> exit 0
```

全量测试只出现项目既有的 `datetime.utcnow` 与 FastAPI `on_event` 弃用警告。最终两个 SQLite 指纹补强发生在 44 项全量之后，已由最新 19 项历史/SQLite/护栏测试单独覆盖；它们不进入 MySQL 方言分支，也不改变迁移 DDL。
