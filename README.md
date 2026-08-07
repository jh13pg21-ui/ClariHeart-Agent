# MindBridge

MindBridge 是一个面向校园心理支持场景的生产化 AI Agent 项目。系统采用 FastAPI、事件驱动多 Agent、分层记忆、混合检索 RAG、事务 Outbox 和 Celery，在本地模型优先的前提下提供风险识别、上下文管理、长期记忆和可靠异步处理。

> 本项目用于工程研究和辅助支持，不替代专业心理咨询、医疗诊断或紧急救援。

## 核心能力

- 事件驱动多 Agent：Coordinator、Understanding、Safety、Context、Response 通过任务板、共享黑板和 artifact 协作。
- 生产级 Prompt：显式 Prompt Registry、语义化 ID、SemVer、严格变量校验、输出 Schema 和 release manifest 哈希。
- 可预算上下文：按目标模型能力估算 token，执行 L0–L3 确定性压缩，并在 Provider 报告超限时执行一次应急压缩。
- 统一模型网关：Ollama 本地模型优先，提供类型化错误、deadline、有界重试、流恢复、输出续写和受控云降级。
- 高风险隐私边界：高风险请求在代码策略层禁止云外发；低/中风险云降级也必须同时满足授权、脱敏和 context manifest 校验。
- 分层记忆：MySQL 保存完整会话和权威摘要，Redis 仅作为短期缓存，长期记忆保存证据、置信度、来源和版本状态。
- 异步记忆任务：摘要刷新、长期记忆提取和记忆整合通过 Transactional Outbox、RabbitMQ、Celery 执行。
- 生产级 RAG 摄取：LiteParse、PaddleOCR、Vision 二维路由、Canonical Document JSON、父子块和版本化原子激活。
- 安全与治理：JWT HttpOnly Cookie、CSRF、Argon2id、角色隔离、工具幂等、限流、死信和隐私保留策略。
- 可观测性：Agent Trace、模型调用元数据、上下文压缩审计和管理员低基数运行指标。

## 系统架构

```text
Browser / API Client
        |
        v
FastAPI + JWT/CSRF
        |
        v
MindBridgeAgentHarness
        |
        v
Event-driven Agent Runtime
  Coordinator / Understanding / Safety / Context / Response
        |
        +--> Prompt Registry + Context Planner
        +--> MySQL / Redis Memory
        +--> Hybrid RAG
        +--> Model Gateway -> Ollama
                          -> OpenAI-compatible fallback（受隐私策略约束）
        |
        v
MySQL transaction + Transactional Outbox
        |
        v
RabbitMQ -> Celery workers
  general / alert / ingestion / beat
```

## Agent Runtime

每轮对话进入事件驱动协作流程：

```text
TURN_STARTED
-> CoordinatorAgent 创建任务
-> UnderstandingAgent 判断 CHAT / CONSULT
-> SafetyAgent 独立判断 LOW / MEDIUM / HIGH
-> ContextAgent 按需加载记忆、Skill 和 RAG
-> ResponseAgent 生成候选回复
-> SafetyAgent 审查候选回复
-> CoordinatorAgent FINAL_ACCEPTED
-> SSE 流式输出
```

每个 Agent task 都有独立超时和重试边界。单个 Agent 失败会产生带错误元数据的保守 artifact，不会直接取消同轮其他任务。中高风险回复必须经过独立安全审查。

## Prompt 与上下文管理

Prompt 文件位于 `app/prompts/`，由代码中的显式注册表管理，不在运行时扫描目录。Registry 会校验：

- Prompt ID 是否唯一；
- 版本是否符合 SemVer；
- 输入变量是否完整且没有多余字段；
- 输出 Schema 是否已注册；
- Prompt 路径是否越界。

`PromptAssembler` 将可信指令与不可信业务数据分区组装，并生成只包含版本、哈希和 section 元数据的 Prompt Manifest，不把 Prompt 正文写入审计表。

`ContextPlanner` 根据 Provider/Model 的 context window、输出预算、恢复预留和安全余量计算输入预算：

- L0：同来源、同类别、同内容去重；
- L1：正常水位下执行分类预算选择；
- L2：结构化会话摘要替换较旧原始消息；
- L3：超出 hard limit 时按优先级压缩到约 70% 水位；
- REACTIVE：Provider 返回 `PROMPT_TOO_LONG` 后，只保留 emergency allowlist，并且每个请求最多执行一次。

必需 section 不会被静默删除。如果必需内容本身超过模型窗口，系统返回明确的 `INPUT_TOO_LARGE` 或 `CONTEXT_UNRECOVERABLE`。

## Model Gateway 与错误恢复

所有 `AiClient` 模型调用通过 `app/llm/` 下的统一契约路由。当前恢复矩阵包括：

| 故障 | 行为 |
|---|---|
| 429 / 529 / 502 / 503 / 504 | 在共享 deadline 内指数退避并加入 jitter |
| Timeout / Network | 有界重试，耗尽后按策略判断是否允许 fallback |
| Prompt too long | 不直接切云，由 Agent 边界执行一次 reactive compact |
| Output truncated | 先增加输出预算，再进行有界续写 |
| Stream interrupted | 缓冲未审核内容，最多恢复一次；必要时发送 replace 事件 |
| Authentication / Permanent | 不重试，返回类型化错误 |

云降级采用 fail-closed 策略：

- `HIGH` 风险永远不能调用云 Provider；
- 请求必须显式允许云外发；
- 消息必须已经脱敏；
- context section manifest 必须完整且无重复；
- 未配置云 Provider 或 API Key 时继续保留本地失败语义，不伪造成功。

## 分层记忆

### 会话记忆

- `chat_sessions`、`chat_messages`：MySQL 权威原始记录；
- Redis：最近消息窗口的可丢失缓存；
- `conversation_memory_summaries`：结构化摘要检查点；
- Outbox 事件 `memory.summary.refresh`：异步触发摘要更新。

摘要调度使用单调 watermark 和事务预留，避免并发重复任务以及旧摘要覆盖新摘要。模型失败、JSON 非法、证据越界或隐私校验失败时，使用确定性摘要降级，不阻断学生端回复。

### 长期记忆 V3

长期记忆记录以下生产元数据：

- 证据消息 ID；
- extraction method；
- Prompt 版本；
- 模型 Provider 与模型名；
- 置信度、确认次数、使用次数；
- 生效时间、过期时间；
- `ACTIVE / SUPERSEDED / EXPIRED` 状态；
- supersedes 版本关系。

V3 进一步增加稳定语义槽位 `memory_key`、冲突组、合并来源谱系和仲裁原因，并支持 `CONFLICTED` 隔离状态。提取模型输出 `CREATE / CONFIRM / SUPERSEDE / CONFLICT / IGNORE` 建议，但服务端会对白名单动作、证据消息 ID、关联记忆 ID 和用户归属做确定性校验，模型不能凭空引用或直接修改数据库。

相同 `memory_key` 下，相同事实会追加证据和确认次数；明确变化会创建新版本并将旧版本标记为 `SUPERSEDED`；旧事实再次得到新证据时可恢复为最新版本。无法可靠判断替代关系的矛盾候选进入 `CONFLICTED` 隔离区，不参与正常检索和 Prompt 注入。注入模型上下文时采用相关性、置信度和时效性组合排序，只有真正进入最终 context plan 的记忆才记录使用次数。

### Memory Dream 与自动整理

Dream 默认启用，由“记忆提取完成后检查”和 Celery Beat 周期扫描两个入口触发，但必须依次通过参考 Claude Code 的四层门控：

1. 距上次成功 Dream 至少 24 小时；
2. 距上次候选扫描至少 60 分钟；
3. 上次 Dream 后至少有 5 个产生长期记忆变更的会话；
4. 获取数据库租约；租约默认 1 小时，崩溃遗留租约可被后续扫描恢复。

项目额外要求至少 10 条新增或更新的活跃记忆，避免小样本无意义调用模型。通过门控后先在同一数据库事务中预留运行记录和租约，再写入 Transactional Outbox，由 RabbitMQ/Celery 执行整合。Outbox 永久投递失败会立即释放租约；Worker 崩溃则由租约过期恢复。

Dream 只允许 `KEEP / MERGE / SUPERSEDE / EXPIRE`。所有决策先整批校验，任一来源 ID、动作或合并内容非法时整轮零记忆变更。`MERGE` 会创建一条新的活跃记忆，保存证据并集、`consolidated_from_ids_json` 来源谱系和合并原因，再将所有来源标记为 `SUPERSEDED`；不会再把第一条来源假装成合并结果，也不会物理删除历史。

```env
MEMORY_CONSOLIDATION_ENABLED=true
MEMORY_CONSOLIDATION_MIN_INTERVAL_HOURS=24
MEMORY_CONSOLIDATION_SCAN_INTERVAL_MINUTES=60
MEMORY_CONSOLIDATION_MIN_MODIFIED_SESSIONS=5
MEMORY_CONSOLIDATION_MIN_ACTIVE_MEMORIES=10
MEMORY_CONSOLIDATION_LEASE_SECONDS=3600
MEMORY_CONSOLIDATION_BEAT_INTERVAL_MINUTES=15
```

## RAG 与文档摄取

新文件摄取位于 `app/rag_ingestion/`：

1. LiteParse 读取原生文字、坐标、图片和页面复杂度证据；
2. 文字路由选择 `NATIVE / PADDLE_OCR / HYBRID`；
3. 结构路由选择 `LOCAL / VISION`；
4. 本地严格 Schema 校验并融合为 Canonical Document JSON；
5. 按章节生成父子块、表格块和媒体语义块；
6. 新版本完成索引校验后原子激活，失败时旧 active 版本继续服务。

查询时融合 Chroma 向量候选与 BM25 候选，再执行本地 rerank。缺少 Chroma、Embedding 不可用且 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，回退到本地 BM25 路径。

管理员私有文件默认禁止调用 Vision。只有上传时明确设置 `cloudVisionAllowed=true` 才允许发送；学生聊天、心理报告和个人记忆不会进入文档 Vision Provider。

## 风险安全与异步工具

风险由 SafetyAgent 独立评估，采用可解释规则安全门、模型 JSON 评估和保守降级。高风险消息会在业务事务内写入心理报告与工具 Outbox：

```text
EXCEL_REPORT
CASE_CREATE -> ALERT_SEND
```

RabbitMQ/Celery 消费保持幂等。任务耗尽重试后写入 `dead_letter_records`；邮件预警支持 `log` 和 `smtp` 模式，并带每分钟限流。

## 可观测性

- `agent_run_traces`：Agent 步骤、artifact 元数据、知识召回引用和最终结果；
- `model_call_traces`：Provider、模型、路由、状态、错误码、token、延迟、重试次数；
- `context_compaction_records`：压缩层级、前后 token、水位线和哈希；
- Prometheus Counter/Histogram：模型、恢复、上下文和记忆任务聚合指标；
- `GET /api/admin/runtime-metrics`：管理员可读的聚合快照和 p50/p95/p99 延迟。

Trace 与指标不保存 Prompt、模型输出、消息正文或用户输入正文。

## 技术栈

| 领域 | 技术 |
|---|---|
| Web/API | Python 3.12、FastAPI、Uvicorn、SSE |
| 数据库 | MySQL 8.0、SQLAlchemy 2、Alembic、PyMySQL |
| 缓存与队列 | Redis、RabbitMQ、Celery、Transactional Outbox |
| 模型 | Ollama、OpenAI-compatible API、Mock Provider |
| Prompt/Context | Jinja2、tiktoken、tokenizers、版本化 Prompt Registry |
| RAG | Chroma、OpenAI Embeddings、BM25、本地 reranker |
| 文档摄取 | LiteParse、PaddleOCR、PaddlePaddle、Vision Provider、pypdf 兼容路径 |
| 安全 | Argon2id、JWT HttpOnly Cookie、CSRF、Fernet 可选静态加密 |
| 可观测性 | SQL Trace、Prometheus client、工程 Harness |
| 其他 | openpyxl、SMTP、MCP |

## 目录结构

```text
app/
├── agents/          # 事件驱动 Agent runtime 与 harness
├── api/             # 认证、聊天、管理员和知识摄取 API
├── cli/             # 迁移、用户和知识库命令
├── context/         # token 估算、规划、压缩和审计
├── context_eval/    # 上下文与错误恢复离线故障评测
├── core/            # 配置、数据库、安全和启动检查
├── harness/         # 一键工程验收
├── knowledge/       # 内置知识文件；PDF 为本地资产
├── llm/             # Provider、Gateway、错误分类与恢复
├── mcp_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── prompts/         # Prompt Registry、模板与输出 Schema
├── rag_eval/        # 检索评测集
├── rag_ingestion/   # 生产文档摄取流水线
├── risk_eval/       # 风险安全门评测
├── schemas/         # API DTO
├── services/        # 领域服务
├── static/          # 原生前端
└── workers/         # Celery tasks 与 Outbox Publisher

migrations/          # Alembic 迁移
skills/              # 内置 Agent Skills
tests/               # pytest / unittest 混合测试
training/            # LoRA 数据与训练脚本
```

## 快速启动：Docker Compose

### 1. 准备配置

```powershell
Copy-Item .env.example .env
```

`.env.example` 当前设置 `AI_PROVIDER=mock`，适合无模型依赖的演示。要使用本地 Ollama，请修改：

```env
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=mindbridge-qwen2.5-7b-ft:latest
```

生产环境必须替换 `JWT_SECRET_KEY`，HTTPS 部署必须设置：

```env
AUTH_SECURE_COOKIE=true
```

### 2. 启动完整服务

```bash
docker compose up -d --build
```

Compose 当前包含：

- `mysql`：MySQL 8.0，宿主机端口 `13306`；
- `redis`：Redis 7.2，宿主机端口 `16379`；
- `rabbitmq`：RabbitMQ 3.13，AMQP 端口 `15673`，管理端口 `15672`；
- `migrate`：启动前执行 Alembic upgrade；
- `app`：FastAPI，端口 `8080`；
- `worker-general`：通用任务、摘要和长期记忆；
- `worker-alert`：邮件预警队列；
- `worker-ingestion`：单并发 RAG 摄取；
- `worker-beat`：Dream 扫描、保留策略等周期任务；
- `outbox-publisher`：发布事务 Outbox。

### 3. 创建账号

生产启动不会自动创建默认账号，也不存在可直接登录的默认密码。首次启动后执行：

```bash
docker compose exec app python -m app.cli.users create admin --display-name "Administrator" --admin
docker compose exec app python -m app.cli.users create student --display-name "Demo Student"
```

命令会交互式要求输入并确认密码。

### 4. 提交内置知识

```bash
docker compose exec worker-ingestion python -m app.cli.knowledge sync-builtins
```

提交是异步且幂等的。命令完成只表示文件、Job 和 Outbox 已可靠保存；文档完成索引并激活后才可检索。

### 5. 查看状态

```bash
docker compose ps
curl http://127.0.0.1:8080/actuator/health
```

## 本地开发

推荐使用 Python 3.12：

```bash
python -m venv .venv
pip install -r requirements-dev.txt
```

本地运行仍需要 MySQL、Redis 和 RabbitMQ。数据库必须先升级到 Alembic head：

```bash
python -m app.cli.migrate upgrade
python -m app.cli.users create admin --admin
python -m app.cli.users create student
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

应用启动只检查数据库是否位于 head，不会自动创建或修改 Schema。

异步组件分别启动：

```bash
celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --queues=mindbridge.general
celery -A app.workers.celery_app:celery_app worker --loglevel=INFO --queues=mindbridge.alert
celery -A app.workers.celery_app:celery_app beat --loglevel=INFO
python -m app.workers.outbox_publisher
```

PaddleOCR 摄取 worker 建议使用 `requirements-ingestion.txt` 或 `Dockerfile.ingestion` 提供的独立环境。

## 主要配置

完整配置见 `.env.example` 和 `app/core/config.py`。

```env
AI_PROVIDER=mock
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mindbridge-qwen2.5-7b-ft:latest
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

MODEL_CLOUD_FALLBACK_ENABLED=true
MODEL_RECOVERY_MAX_TRANSIENT_RETRIES=2
MODEL_RECOVERY_MAX_STREAM_RETRIES=1
MODEL_RECOVERY_DEADLINE_SECONDS=65

PROMPT_REGISTRY_ENABLED=true
CONTEXT_PLANNER_ENABLED=true
CONTEXT_PLANNER_SHADOW_MODE=false

MEMORY_COMPACTION_ENABLED=true
LONG_TERM_MEMORY_ENABLED=true
MEMORY_CONSOLIDATION_ENABLED=true
MEMORY_CONSOLIDATION_MIN_INTERVAL_HOURS=24
MEMORY_CONSOLIDATION_SCAN_INTERVAL_MINUTES=60
MEMORY_CONSOLIDATION_MIN_MODIFIED_SESSIONS=5
MEMORY_CONSOLIDATION_MIN_ACTIVE_MEMORIES=10
MEMORY_CONSOLIDATION_LEASE_SECONDS=3600
MEMORY_CONSOLIDATION_BEAT_INTERVAL_MINUTES=15

KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
RAG_INGESTION_ENABLED=true
RAG_OCR_ENABLED=true
RAG_VISION_ENABLED=true
```

`MODEL_GATEWAY_ENABLED`、`RECOVERY_ORCHESTRATOR_ENABLED`、`MEMORY_V2_ENABLED` 和 `MEMORY_V2_SHADOW_MODE` 当前存在于 Settings，作为发布状态声明；现有代码没有用它们切换到旧实现。真正参与运行分支的开关包括 `MODEL_CLOUD_FALLBACK_ENABLED`、`PROMPT_REGISTRY_ENABLED`、`CONTEXT_PLANNER_ENABLED`、`CONTEXT_PLANNER_SHADOW_MODE` 和 `MEMORY_CONSOLIDATION_ENABLED`。

Docker Compose 只会把 `docker-compose.yml` 中显式列出的变量传入对应容器；仅在 `.env` 增加一个未映射变量不会自动改变容器内 Settings。

## 本地 GGUF / Ollama

模型目录：

```text
models/mindbridge-qwen2.5-7b-ft/
├── Modelfile
└── mindbridge-qwen2.5-7b-ft-q4_k_m.gguf   # 本地文件，不提交 Git
```

Linux/macOS 脚本：

```bash
UPSTREAM_GGUF=/path/to/model.gguf ./scripts/create-finetuned-model.sh
./scripts/start-ollama.sh
```

也可以使用已有 Ollama 模型，只需设置 `OLLAMA_MODEL`。模型状态可通过 `GET /api/agent/status` 查看。

## OpenAI-compatible Provider

```env
AI_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=你的_API_Key
OPENAI_MODEL=gpt-4o-mini
```

高风险请求禁止云外发，因此面向完整风险场景的部署应保留可用的本地 Ollama Provider。知识库 Embedding 和 RAG Vision 具有独立配置，但可复用 OpenAI-compatible base URL 与 API Key。

## 本地 PDF 与 Gold Data

`app/knowledge/pdf/*.pdf` 被 `.gitignore` 和 `.dockerignore` 排除，不会上传到 GitHub，也不会随新 clone 或 Git worktree 自动出现。

当前 RAG gold dataset 对应 5 份本地 PDF、270 个页级路由标签和 30 个深度代表页。JSON 标注文件会提交到 Git，PDF 二进制由开发者在本地单独准备，并放入：

```text
app/knowledge/pdf/
```

缺少 PDF 时，普通代码与核心机制测试仍可运行，但必须排除依赖文件哈希的测试：

```bash
python -m pytest -q --ignore=tests/rag_ingestion/test_evaluation.py
```

本地 5 份 PDF 齐全时运行完整测试：

```bash
python -m pytest -q
python -m app.rag_ingestion.evaluation.runner --validate-only
```

当前页级标签包含机器种子标注，评测 CLI 会在人工复核前阻止其被当作正式发布金标。

## API 概览

### 认证与学生端

```text
POST   /api/auth/login
POST   /api/auth/refresh
POST   /api/auth/logout
POST   /api/chat/stream
GET    /api/profile
GET    /api/conversations
GET    /api/conversations/{sessionId}
GET    /api/memories
DELETE /api/memories/{memoryId}
GET    /api/privacy/preferences
PATCH  /api/privacy/preferences
```

### 管理端

```text
GET  /api/admin/reports
GET  /api/admin/alerts
GET  /api/admin/cases
GET  /api/admin/tool-jobs
GET  /api/admin/dead-letters
GET  /api/admin/outbox-events
GET  /api/admin/agent-traces
GET  /api/admin/runtime-metrics
```

### 知识摄取

```text
POST /api/admin/knowledge/files
GET  /api/admin/knowledge/documents
GET  /api/admin/knowledge/documents/{documentId}
GET  /api/admin/knowledge/documents/{documentId}/pages
GET  /api/admin/knowledge/jobs
GET  /api/admin/knowledge/jobs/{jobId}
POST /api/admin/knowledge/jobs/{jobId}/retry
```

除登录外，写请求需要 Cookie 会话和 `X-CSRF-Token`。

## 测试与工程验收

安装测试依赖：

```bash
pip install -r requirements-dev.txt
```

没有本地 PDF 的标准 GitHub clone：

```bash
python -m pytest -q --ignore=tests/rag_ingestion/test_evaluation.py
```

工程 Harness：

```bash
python -m app.harness.runner --json
```

它覆盖 Risk Safety、Agent Routing、Standard Skills、Structured Memory、RAG 和 API 六条核心链路，报告写入 `target/harness/`。

上下文与恢复故障评测：

```bash
python -m app.context_eval.runner --json
```

版本化数据集覆盖正常请求、Prompt 超限、429、529、超时、非法 JSON、流中断、Redis 不可用和高风险过载。该 Harness 完全离线，使用确定性故障 Provider；它用于验证恢复策略，不代替真实 Provider 压测。

风险评测：

```bash
python -m app.risk_eval.runner
```

RAG 检索评测：

```bash
AI_PROVIDER=mock python -m app.rag_eval.runner
```

MySQL destructive migration tests 只允许针对名称以 `mindbridge_test_` 开头的隔离数据库，并要求双重确认：

```env
MINDBRIDGE_TEST_MYSQL_URL=mysql+pymysql://root:password@127.0.0.1:3306/mindbridge_test_migrations
MINDBRIDGE_ALLOW_DESTRUCTIVE_DB_TESTS=1
```

```bash
python -m pytest -q tests/test_migrations_mysql.py
```

## MCP 工具服务

```bash
python -m app.mcp_tools.server
```

当前工具包括 Excel 台账、个案创建、预警发送/确认和个案备注。业务应用不会绕过 Outbox 直接执行这些副作用；MCP 作为独立集成入口也受相同工具治理边界约束。

## 训练

`training/` 提供数据去重、固定随机种子、标签分层切分、LoRA 训练和独立 test split 评估。训练依赖与完整命令见 `training/README.md` 和 `requirements-training.txt`。

## 当前边界

- 系统是工程辅助工具，不提供临床诊断结论；风险评测指标也不代表临床有效性。
- 本地 PDF、GGUF 权重、`.env`、密钥、数据库和运行产物不会提交到 Git。
- Dream 的冲突识别和合并内容仍依赖模型判断，但所有 ID、动作、隐私边界和状态变更都由服务端校验；不确定冲突会隔离而非注入上下文。
- RAG gold page labels 尚包含机器种子标注，必须人工复核后才能作为发布门槛。
- Prompt Registry 能生成 release manifest 并校验显式传入的历史记录，但当前应用启动流程只检查认证配置与数据库迁移状态，没有自动比对一个独立的锁定 manifest 文件。
- 离线故障 Harness 验证确定性恢复矩阵，不等价于真实网络、真实 Redis 集群或真实模型的容量测试。

## 面试讲述建议

可以围绕以下四个工程问题展开：

1. 如何把 Prompt 拼接升级为版本化、可信边界清晰且可审计的上下文系统；
2. 如何依据模型窗口做确定性压缩，并限制 reactive recovery 的次数；
3. 如何用稳定语义槽位、冲突隔离、证据谱系、四层 Dream 门控和可恢复租约控制长期记忆污染；
4. 如何在 deadline、幂等、隐私出站策略和降级矩阵约束下恢复模型及基础设施故障。

对应代码主要位于 `app/prompts/`、`app/context/`、`app/llm/`、`app/services/long_term_memory.py`、`app/services/memory_consolidation.py` 和 `app/context_eval/`。
