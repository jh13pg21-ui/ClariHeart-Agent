# MindBridge 生产化加固与异步多 Agent 改造设计

日期：2026-07-26  
状态：已获用户批准，待实施  
基线提交：`90e354f17916c4f4041ada1a9b67fc94e028e93e`

## 1. 背景

MindBridge 当前已经具备 FastAPI、事件驱动多 Agent、心理风险识别、RAG、Redis 短期记忆、MySQL 持久化、标准 Skills、SSE 回复、后台报告以及工具任务等完整能力。

当前实现仍存在以下生产阻塞问题：

- `ReportService` 方法缩进错误导致多个报告、会话、Trace 和工具审计接口返回 500。
- 多 Agent 调度名义上按任务认领，实际通过同步 `for` 循环串行执行。
- SafetyAgent 审核候选 Prompt，而不是学生最终看到的模型文本。
- 高风险工具任务在完整 SSE 回复结束后才提交，客户端断开或模型异常可能阻止预警入队。
- 工具治理服务没有接入实际队列执行链。
- 认证使用 Basic Auth、固定演示账号和无盐 SHA-256 密码。
- 工具队列运行在 FastAPI 进程内，不适合多实例和可靠投递。
- 生产启动依赖 `create_all()`，缺少数据库版本迁移。
- 输入、文件上传、隐私脱敏、Trace 和错误恢复仍需加固。

本设计在保持现有业务能力和数据库数据无损的前提下，将项目升级为可进行生产试点的架构。

## 2. 已确认的设计决策

1. 采用渐进式生产化改造，保留 FastAPI 单体边界，不拆分为多个业务微服务。
2. 认证采用生产优先方案，移除 Basic Auth 和硬编码默认账号。
3. 使用 JWT Access Token、可轮换 Refresh Token 和 Argon2id 密码哈希。
4. 使用 RabbitMQ + Celery 作为可靠异步任务系统。
5. 使用 MySQL Transactional Outbox 保证报告事务与消息投递的一致性。
6. Redis 继续负责短期记忆、缓存、限流和 Token 撤销状态。
7. 高风险回复不进行未审核的逐 Token 输出，必须完整生成并审核后返回。
8. CHAT、LOW 和通过安全策略的 MEDIUM 场景继续支持 SSE。
9. 管理员继续允许查看完整会话，但每次访问必须留下安全审计。
10. 现有 MySQL 用户、会话、报告、个案和知识数据必须无损迁移。

## 3. 目标

### 3.1 功能目标

- 修复当前后台接口故障。
- 实现阶段内并行、阶段间有序的异步多 Agent Runtime。
- 让 ResponseAgent 生成真实候选文本，SafetyAgent 审核真实输出。
- 高风险报告与 Outbox 事件先于回复发送可靠落库。
- 将 Excel、个案和高风险预警迁移到独立 Celery Worker。
- 将工具授权、审计、重试、死信和幂等接入真实执行链。
- 将认证升级为生产级 JWT 会话体系。
- 通过 Alembic 对现有数据库进行无损迁移。
- 完善输入限制、隐私保护、审计和可观测性。

### 3.2 质量目标

- 高风险客户端断开连接时，预警仍能进入可靠处理链。
- RabbitMQ 短暂不可用时，Outbox 事件不丢失。
- Celery 重复投递或重复消费时不产生重复业务副作用。
- Ollama 或 OpenAI 超时时，高风险场景使用安全模板。
- 并行 Agent 不共享非并发安全的 SQLAlchemy Session。
- 所有核心 API、迁移、认证、安全和队列路径均有自动化测试。

## 4. 非目标

- 本次不拆分认证、Agent、知识库和风险管理微服务。
- 本次不引入 Kubernetes。
- 本次不建立学生自助注册、邮件找回密码或学校统一身份认证。
- 本次不改变管理员可查看完整会话的产品权限。
- 本次不训练或重新量化 GGUF 模型。
- 本次不使用 RabbitMQ 保存业务权威状态。

## 5. 总体架构

```text
Browser
  │ JWT HttpOnly Cookie + CSRF
  ▼
FastAPI
  ├─ Auth API
  ├─ Chat/SSE API
  ├─ Admin API
  ├─ Knowledge API
  └─ Async Agent Runtime
       ├─ Stage 1: Intent + Risk + Memory Prefetch
       ├─ Stage 2: Context + RAG + Skills
       ├─ Stage 3: Response Generation
       └─ Stage 4: Actual Output Safety Review
  │
  ├─ MySQL
  │    ├─ Business Tables
  │    ├─ Auth Sessions
  │    ├─ Security Audits
  │    ├─ Tool Audits
  │    └─ Transactional Outbox
  ├─ Redis
  │    ├─ Short-term Memory
  │    ├─ Rate Limiting
  │    └─ Token Revocation Cache
  └─ Ollama/OpenAI

Outbox Publisher
  │ Publisher Confirm
  ▼
RabbitMQ
  ├─ mindbridge.general
  ├─ mindbridge.alert.high
  ├─ mindbridge.retry
  └─ mindbridge.dead_letter
       │
       ├─ General Celery Worker
       └─ High-Risk Celery Worker
```

FastAPI 进程不再启动进程内工具线程池。Worker 和 Outbox Publisher 使用独立容器和独立数据库连接。

## 6. 认证与权限

### 6.1 密码

- 使用 `argon2-cffi` 提供 Argon2id 密码哈希。
- 新密码不允许使用旧 SHA-256 哈希。
- 现有用户记录保留，迁移时增加 `password_algorithm` 和 `must_reset_password`。
- 旧账号迁移后不能使用旧密码直接登录，必须通过管理 CLI 设置新密码。
- 移除应用启动时自动创建 `admin/admin123` 和 `student/student123` 的逻辑。

管理命令：

```text
python -m app.cli.users create --username <name> --role ROLE_ADMIN
python -m app.cli.users set-password --username <name>
python -m app.cli.users disable --username <name>
```

密码通过交互式安全输入读取，不允许作为普通命令行参数传递。

### 6.2 Token 会话

- Access Token 默认有效期 15 分钟。
- Refresh Token 默认有效期 7 天。
- Refresh Token 每次刷新后旋转，旧 Token 立即撤销。
- Refresh Token 只以哈希形式保存到 `auth_sessions`。
- 注销、密码重设和账号禁用会撤销该用户全部 AuthSession。
- JWT 必须包含 `sub`、`roles`、`session_id`、`iat`、`exp` 和 `jti`。
- JWT 密钥只能从环境变量或 Secret Manager 提供，不写入仓库。

### 6.3 浏览器安全

- Access 和 Refresh Token 通过 `HttpOnly` Cookie 传输。
- 生产 Cookie 必须启用 `Secure`。
- 默认使用 `SameSite=Lax`，部署在同站点时可配置为 `Strict`。
- 所有状态修改请求要求 CSRF Header 与 CSRF Cookie 匹配。
- 前端不再使用 `sessionStorage` 保存认证凭据。
- CORS 默认关闭跨域；需要跨域时使用明确白名单且禁止通配凭据。

### 6.4 权限和审计

- 保留 `ROLE_USER` 与 `ROLE_ADMIN`。
- 学生只能访问自己的 Session 和报告。
- 管理员可访问报告、个案和完整会话。
- 管理员读取完整会话时写入 `security_audit_records`，记录操作者、目标 Session、时间、请求 ID 和结果。

## 7. 数据库无损迁移

### 7.1 Alembic

- 新数据库通过 Alembic 从零创建完整 Schema。
- 现有数据库先识别 MindBridge 基线表，再写入基线版本并执行增量迁移。
- 自动识别仅接受已知表结构；结构不匹配时停止迁移，不尝试猜测。
- 迁移脚本不得删除现有用户、会话、报告、知识、个案或工具记录。
- 生产应用启动前执行显式迁移命令，FastAPI 启动不自动修改 Schema。

### 7.2 新增表

- `auth_sessions`
- `security_audit_records`
- `outbox_events`
- `processed_messages`

### 7.3 现有表调整

- `user_accounts`：增加密码算法、强制重设、禁用状态和密码更新时间。
- `tool_jobs`：增加消息 ID、幂等键和 Broker 状态映射。
- `tool_audit_records`：记录授权、开始、成功、失败和阻止状态。
- 时间字段逐步迁移为带时区 UTC；兼容读取旧的无时区数据。
- 为报告、个案、任务和知识块增加必要的唯一约束和索引。

### 7.4 迁移验证

迁移前后自动比较以下数据量：

- 用户
- 会话
- 消息
- 心理报告
- 风险个案
- 知识块
- Excel、预警和任务记录

数量下降或关键外键失效时迁移判定失败。

## 8. 异步多 Agent Runtime

### 8.1 接口

- `AutonomousAgent.act()` 改为异步接口。
- `EventDrivenCoordinator.run()` 改为异步接口。
- `EventDrivenAgentRuntimeService.run()` 改为异步接口。
- `MindBridgeAgentHarness.run()` 改为异步接口。
- `AiClient.complete()` 使用 `httpx.AsyncClient`。

### 8.2 执行阶段

#### Stage 1：并行分析

同一份只读 Blackboard 快照上并行运行：

- UnderstandingAgent 意图识别
- SafetyAgent 首次风险评估
- ContextAgent 记忆预取

使用 `asyncio.TaskGroup` 管理任务生命周期。结果完成后由 Coordinator 按以下确定顺序合并：

1. Safety Override
2. Risk Artifact
3. Intent Artifact
4. Memory Artifact

任一 HIGH Safety Override 都不能被后续低风险结果覆盖。

#### Stage 2：上下文

- `CHAT + LOW` 跳过 RAG。
- CONSULT、RISK、MEDIUM、HIGH 进入上下文阶段。
- 查询改写、Skill 选择和可独立执行的本地准备并行运行。
- RAG 检索依赖最终查询，但向量召回和 BM25 召回可在 KnowledgeService 内部并行。

#### Stage 3：候选回复

- ResponseAgent 使用其 AgentModelProfile 调用模型。
- 输出为 `ResponseCandidateArtifact`，包含候选文本、模型信息、耗时和 Prompt 摘要哈希。
- Prompt 全文不默认写入普通 Trace。

#### Stage 4：实际输出安全审核

SafetyAgent 审核真实候选文本，产生：

- `APPROVED`
- `REVISE`
- `FALLBACK`

审核包含：

- 危险操作和方法细节检测
- 风险等级、情绪分数、置信度和后台报告泄露检测
- 高风险回复必要安全要素检测
- 空回复、异常截断和模型错误检测
- 可配置的 LLM 二次审核

最多进行一次修订。第二次仍未通过时使用安全模板。

### 8.3 并发数据安全

- 并行 Agent 不共享同步 SQLAlchemy Session。
- Runtime 开始前预加载只读业务数据。
- 必须查询数据库的独立任务创建独立 Session。
- 最终结果由 Harness 在一个明确事务中持久化。
- Blackboard 和 Artifact 保持不可变风格。
- 合并过程只在 Coordinator 中执行。

## 9. 分级输出与高风险闭环

### 9.1 CHAT/LOW

- 使用基础规则审核。
- 保持 SSE Token 流式输出。
- 流式异常时发送标准错误事件并记录 Trace。

### 9.2 MEDIUM

- 先生成完整候选文本并通过审核。
- 审核通过后可按安全段落通过 SSE 输出。
- 产生心理报告、Excel 任务和风险个案任务。

### 9.3 HIGH

执行顺序固定为：

1. 完成硬规则和模型风险评估。
2. 在一个 MySQL 事务中保存用户消息、心理报告、Trace 初始记录和 Outbox 事件。
3. 提交事务。
4. 生成完整候选回复。
5. SafetyAgent 审核真实文本。
6. 审核通过后一次性发送。
7. 模型、审核或超时失败时发送经过测试的高风险安全模板。
8. 保存最终实际发送文本并完成 Trace。

高风险 Outbox 事件不依赖浏览器连接和模型回复是否成功。

## 10. RabbitMQ、Celery 与 Transactional Outbox

### 10.1 队列

- `mindbridge.general`：Excel 与普通个案任务。
- `mindbridge.alert.high`：高风险预警，使用独立 Worker。
- `mindbridge.retry`：带延迟的重试任务。
- `mindbridge.dead_letter`：达到最大重试次数的消息。

队列和消息均持久化。高风险队列配置高优先级或独立消费资源。

### 10.2 Outbox

业务事务写入 `outbox_events`：

- `event_id`
- `event_type`
- `aggregate_type`
- `aggregate_id`
- `payload_json`
- `status`
- `attempts`
- `available_at`
- `published_at`
- `last_error`

Payload 只包含业务 ID、风险等级和幂等信息，不包含完整学生原文。

Outbox Publisher：

- 使用数据库行锁或 `SKIP LOCKED` 支持多实例。
- 使用 RabbitMQ Publisher Confirm。
- 确认成功后标记已发布。
- 失败时指数退避。
- Publisher 崩溃导致重复发布时由消费者幂等处理。

### 10.3 Celery Worker

统一执行流程：

1. 读取消息 ID 与幂等键。
2. 检查 `processed_messages`。
3. 加载报告或个案。
4. 调用 ToolGovernance 授权。
5. 写入 AUTHORIZED 或 BLOCKED 审计。
6. 执行业务工具。
7. 写入 SUCCESS 或 FAILED。
8. 成功后写入 `processed_messages` 并 ACK。
9. 可重试错误进入重试队列。
10. 达到上限进入死信队列和 `dead_letter_records`。

`case.create` 成功后发布 `case.created`，高风险预警消费该事件，避免依赖数据库轮询。

## 11. API、输入与文件安全

- Chat 消息最大长度默认 4000 字符，可通过环境变量配置。
- Session ID 必须满足固定长度和字符集。
- 知识 source 和 content 增加长度限制。
- 上传文件仅允许 `.md`、`.txt`、`.pdf`。
- 默认上传上限 10 MiB。
- PDF 增加最大页数和最大提取文本长度。
- 登录、刷新、聊天和管理写接口增加用户/IP 维度限流。
- 错误响应不暴露堆栈、数据库详情、密钥或内部 Prompt。
- SSE 明确定义 `meta`、`token`、`message`、`error` 和 `done` 事件。

## 12. 隐私与数据访问

- 原始会话继续保存到 MySQL，满足管理员完整查看要求。
- 进入模型、Redis、Agent 私有记忆和 Trace 摘要的数据必须脱敏。
- 脱敏增加学号、银行卡、IPv4、微信/QQ 等可确定模式。
- Prompt、Token、Cookie、SMTP 密码和 RabbitMQ 密码不得写入日志。
- RabbitMQ 消息不传递完整学生原文。
- 管理员查看完整会话必须生成访问审计。
- 数据保留周期通过配置实现，不在本次改造中自动删除现有数据。

## 13. 可观测性

- 每个 HTTP 请求生成 `request_id`。
- 每个 Agent Turn 保留 `turn_id`。
- Trace 记录每个阶段耗时、模型、降级原因和 RAG 命中摘要。
- 移除 `AgentStep` 与 Collaboration Event 的重复序列化。
- Outbox、RabbitMQ、Celery 和工具执行均使用同一个 `event_id` 关联。
- 管理端提供任务、死信、工具审计和安全访问审计查询接口。
- 日志采用结构化字段，但不记录完整敏感正文。

## 14. 测试策略

### 14.1 单元测试

- ReportService 方法与 DTO 转换。
- JWT 签发、过期、轮换、撤销和角色校验。
- Argon2id 密码验证。
- CSRF 校验。
- Risk Output Safety Review。
- 工具治理授权和审计状态机。
- Outbox 状态转换与幂等。
- Agent Artifact 合并优先级。
- 输入和文件限制。

### 14.2 集成测试

- Alembic 新数据库建库。
- 现有 Schema 基线识别和无损增量升级。
- FastAPI 登录、刷新、注销和完整 API 权限。
- RabbitMQ Publisher Confirm。
- Celery 成功、重试、重复消费和死信。
- 客户端断开后高风险 Outbox 仍存在并投递。
- Ollama 超时后高风险模板兜底。
- 管理员读取完整会话后产生审计记录。

### 14.3 Agent 与安全评测

- Stage 1 并行执行验证。
- CHAT、CONSULT、MEDIUM 和 HIGH 路由。
- 真实候选文本安全审核。
- 后台标签泄露测试。
- 危险细节拒绝测试。
- 60 条现有 RAG 数据集回归。
- Engineering Harness 保持并扩展为生产依赖可选的测试套件。

### 14.4 CI

CI 至少执行：

- Python 编译检查
- 单元测试
- SQLite 快速 API 测试
- MySQL 迁移测试
- RabbitMQ/Celery 集成测试
- Harness

生产 Docker 镜像不复制测试；CI 使用独立 Test Stage。

## 15. 部署与回滚

### 15.1 Docker Compose

新增：

- RabbitMQ 服务及健康检查
- General Worker
- High-Risk Worker
- Outbox Publisher

FastAPI、Worker 和 Publisher 共享同一代码镜像，但使用不同启动命令。

### 15.2 上线顺序

1. 备份 MySQL 和 Chroma。
2. 停止旧 App 写入。
3. 运行迁移预检查。
4. 执行 Alembic 升级。
5. 启动 RabbitMQ、Worker 和 Publisher。
6. 启动新 App。
7. 通过 CLI 重设生产账号密码。
8. 执行健康、认证、聊天、高风险和队列烟雾测试。

### 15.3 回滚

- 应用和 Worker 可回滚到前一镜像。
- 数据库迁移优先采用向前修复；新增表和字段不在紧急回滚时删除。
- 未发布 Outbox 事件保留。
- RabbitMQ 中未确认消息不丢弃。
- 回滚前停止新 Worker 消费，避免不同版本同时处理同一事件。

## 16. 验收标准

以下条件必须全部满足：

1. 现有 500 报告接口全部恢复。
2. Basic Auth 和默认账号在生产路径中彻底移除。
3. 现有数据库数据无损迁移。
4. 高风险报告和 Outbox 在回复发送前提交。
5. 客户端断开不影响高风险任务投递。
6. SafetyAgent 审核实际候选文本。
7. 高风险模型失败时返回安全模板。
8. Stage 1 Agent 任务真实并发执行。
9. RabbitMQ/Celery 重复消费不产生重复副作用。
10. 工具授权、执行和结果均可审计。
11. 管理员查看完整会话产生安全审计。
12. 现有 RAG 指标不低于基线容差。
13. 单元测试、集成测试和 Engineering Harness 全部通过。

