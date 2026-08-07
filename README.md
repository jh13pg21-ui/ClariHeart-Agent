# MindBridge Python

## 核心能力

- 学生端 SSE 流式聊天，前端可展示打字机式输出。
- Argon2id 密码哈希与 JWT HttpOnly Cookie 登录，支持学生和管理员角色隔离、刷新令牌轮换与 CSRF 防护。
- 事件驱动多 Agent 协作 runtime：Coordinator、Understanding、Safety、Context、Response 通过共享黑板、任务认领和安全审查协作。
- 动态路由 RAG：意图只判断 `CHAT / CONSULT`，风险独立判断 `LOW / MEDIUM / HIGH`；普通低风险聊天不查知识库，咨询或中高风险场景进入检索增强。
- Chroma 向量 RAG 知识库：支持 Markdown、txt、PDF 文件上传，自动切块，使用 `text-embedding-3-small` 写入向量库，并与 BM25 关键词召回融合后进入本地 reranker；向量不可用时保留本地 BM25 + 词面检索兜底。
- 心理风险评估：可解释安全门优先、LLM JSON 评估、保守规则兜底，并附带独立风险评测集和混淆矩阵。
- 后台报告：记录情绪标签、情绪分数、风险等级、置信度和摘要，但学生端不展示后台评估结果。
- 分层记忆：完整对话写入 MySQL，Redis 保存短期窗口；本地 LLM 通过 Outbox/Celery 异步生成带证据消息 ID 的结构化会话摘要，以 MySQL 为权威检查点、Redis 为缓存，并保留确定性摘要降级。
- 数据闭环：咨询/风险消息完整写入 MySQL，高风险消息写入 Excel 台账并通过邮件发送预警。
- 本地微调模型接入：支持通过 Ollama 加载 `mindbridge-qwen2.5-7b-ft-q4_k_m.gguf`。
- OpenAI-compatible API 接入：也可切换到云端模型。
- RabbitMQ / Celery 异步后处理：通过事务 Outbox 可靠发布 Excel 台账、个案创建和风险通知任务；MCP 工具服务保留为独立集成入口。
- RAG 评测：Recall@K、Precision@K、MRR、NDCG@K、HitRate。

## 技术栈

```text
语言：Python
Web 框架：FastAPI
服务运行：Uvicorn / ASGI
数据库：MySQL，SQLAlchemy ORM，PyMySQL 驱动
短期记忆：Redis
配置管理：pydantic-settings，.env
AI 接入：Ollama，本地微调 GGUF 模型，OpenAI-compatible API，Mock Provider
Agent 编排：事件驱动黑板协作 runtime
RAG：本地知识库切块、OpenAI Embeddings、Chroma 向量库、BM25、分数融合、本地 reranker、上下文扩展
流式输出：Server-Sent Events
文档解析：pypdf
Excel 台账：openpyxl
邮件预警：SMTP / smtplib
前端：原生 HTML / CSS / JavaScript
认证：Argon2id、JWT HttpOnly Cookie、CSRF
异步任务：RabbitMQ、Celery、Transactional Outbox
工具协议：MCP（独立集成入口）
```

说明：当前 Python 版只保留事件驱动多 Agent runtime，入口在 `app/agents/event_driven_runtime.py`。共享返回类型定义在 `app/agents/result.py`。RAG 默认使用 Chroma 本地持久化向量库做语义召回，同时用 BM25 做关键词召回，再融合并本地 rerank；未安装 Chroma、未配置 `OPENAI_API_KEY` 或向量服务异常时，会自动回退到本地 BM25 + `hybrid_score` reranker，避免演示环境中断。

## 目录结构

```text
app/
├── agents/          # 事件驱动多 Agent runtime
├── api/             # FastAPI 路由
├── core/            # 配置、数据库、安全、启动初始化
├── knowledge/       # 内置校园心理知识库
├── mcp_tools/       # MCP 工具服务
├── models/          # SQLAlchemy 实体
├── rag_eval/        # RAG 评测脚本和数据集
├── schemas/         # Pydantic DTO
├── services/        # AI、聊天、知识库、评估、报告、工具服务
└── static/          # 原生前端页面

models/mindbridge-qwen2.5-7b-ft/
├── Modelfile        # Ollama 模型定义
└── README.md        # GGUF 模型放置说明

scripts/
├── run-dev.sh
├── start-ollama.sh
├── create-finetuned-model.sh
└── package-release.sh
```

## Agent loop

每轮对话默认进入事件驱动多 Agent 协作 runtime。Coordinator 维护共享黑板和任务板，专业 Agent 根据能力和置信度认领任务，发布 artifact，再由安全审查和最终采纳机制收敛输出：

```text
TURN_STARTED
-> CoordinatorAgent 创建任务
-> UnderstandingAgent / SafetyAgent / ContextAgent / ResponseAgent 认领任务并发布 artifact
-> SafetyAgent 审查候选回复
-> CoordinatorAgent FINAL_ACCEPTED
-> SSE 流式输出
```

各 Agent 分工：

- `CoordinatorAgent`：维护任务板、预算、安全门槛、冲突仲裁和最终采纳。
- `UnderstandingAgent`：判断 `CHAT / CONSULT`，发布 intent artifact；风险维度完全由 SafetyAgent 独立负责。
- `SafetyAgent`：独立评估风险，必要时发布 `SAFETY_OVERRIDE`，并审查候选回复。
- `ContextAgent`：按需聚合 Redis / MySQL 记忆、RAG 检索结果和 Skill 约束。
- `ResponseAgent`：根据黑板 artifact 生成候选回复正文，等待安全审查和采纳。

每个 Agent task 都在独立超时与重试边界中执行。本地 Ollama 连接失败、超时和上下文溢出会分类处理，采用指数退避与抖动重试；上下文溢出只压缩该任务的黑板视图。重试耗尽后发布带故障元数据的保守 artifact，单个 Agent 异常不会取消同轮其他 Agent，也不会直接中断整个 loop。中高风险回复必须先获得 ContextAgent 的 Skill/RAG artifact。

## 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 已包含：

```text
chromadb
pymysql
redis
```

`AGENT_FRAMEWORK` 仍会读取环境变量，但当前只支持 `event_driven_multi_agent`。历史值或未知值会在状态接口中标记为 fallback，并实际使用事件驱动 runtime。

## MySQL 和 Redis 配置

系统默认使用 MySQL 保存完整业务数据和完整聊天消息，使用 Redis 保存短期对话记忆。启动服务前先创建数据库：

```sql
CREATE DATABASE mindbridge DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'mindbridge'@'%' IDENTIFIED BY 'mindbridge';
GRANT ALL PRIVILEGES ON mindbridge.* TO 'mindbridge'@'%';
FLUSH PRIVILEGES;
```

`.env` 中配置连接：

```env
DATABASE_URL=mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_MEMORY_TTL_SECONDS=86400
REDIS_MEMORY_MAX_MESSAGES=40
MEMORY_COMPACTION_ENABLED=true
MEMORY_COMPACTION_RECENT_MESSAGES=8
MEMORY_SUMMARY_REFRESH_MESSAGES=4
MEMORY_SUMMARY_MAX_CHARS=500
MEMORY_SUMMARY_LLM_ENABLED=true
MEMORY_SUMMARY_LLM_ATTEMPTS=2
MEMORY_SUMMARY_MAX_SOURCE_MESSAGES=40
MEMORY_SUMMARY_INPUT_MAX_CHARS=12000
LONG_TERM_MEMORY_ENABLED=true
LONG_TERM_MEMORY_MAX_ITEMS=200
LONG_TERM_MEMORY_RELEVANT_ITEMS=5
LONG_TERM_MEMORY_EXTRACT_MESSAGES=10
LONG_TERM_MEMORY_EXTRACT_MIN_NEW_MESSAGES=6
```

完整聊天记录写入 MySQL 的 `chat_sessions`、`chat_messages` 等表。Redis 只保存每个会话最近 `REDIS_MEMORY_MAX_MESSAGES` 条短期上下文，并通过 `REDIS_MEMORY_TTL_SECONDS` 自动过期。

短期摘要采用异步增量压缩：保留最近 `MEMORY_COMPACTION_RECENT_MESSAGES` 条原始消息；累计 `MEMORY_SUMMARY_REFRESH_MESSAGES` 条可压缩消息后，在回复落库的同一事务中写入 `memory.summary.refresh` Outbox 事件，经 RabbitMQ 交给通用 Celery worker。worker 使用当前 `AI_PROVIDER`（本地部署默认 Ollama）把旧摘要和新增消息更新成严格 JSON，保存关注主题、互动偏好、明确有效的支持方式、待继续事项和安全连续性，并要求每项携带可校验的证据消息 ID。权威检查点写入 MySQL 的 `conversation_memory_summaries`，脱敏后的副本缓存到 Redis；新一轮 `ContextAgent` 读取结构化摘要和最近原文。模型超时、输出格式错误、证据 ID 越界或隐私校验失败时，系统自动使用确定性摘要，不阻断聊天。

结构化摘要不保存联系方式、身份证、地址、密码、诊断标签、风险等级或自杀自残具体方法。安全相关原话只折叠为“存在需要后续关注的安全信号”，且不会取代 SafetyAgent 对本轮输入的独立风险复核。摘要生成是异步任务，worker 尚未完成时会临时使用上一版摘要和未压缩原文，因此不会增加学生端首 Token 延迟。

跨会话长期记忆写入 MySQL 的 `long_term_memories` 表。每轮回复落库后，
Outbox 经 RabbitMQ 将 `memory.extract` 任务交给通用 Celery worker；worker
只沉淀稳定背景、互动偏好、明确有效的支持方式和长期事项。新一轮对话会加载
记忆索引，并按当前输入选择最多 `LONG_TERM_MEMORY_RELEVANT_ITEMS` 条全文注入
ContextAgent。手机号、邮箱、身份证、高风险原话和诊断标签不会保存，学生可在
学生端查看并删除自己的长期记忆，也可关闭长期记忆并一次性清空已有内容。即使记忆条目少于注入上限，也必须经过模型索引选择或关键词相关性筛选，不再默认全量注入。

默认仅持久化脱敏后的输入；可配置 Fernet 应用层静态加密。Celery Beat 每日执行保留策略：普通聊天正文默认保留 365 天，中高风险安全记录默认保留 1095 天，过期后清除正文而保留必要审计元数据。

## Docker Compose 一键启动

仓库提供 `Dockerfile` 和 `docker-compose.yml`，会启动：

- `mysql`：MySQL 8.4，容器内端口 `3306`，宿主机映射 `13306`
- `redis`：Redis 7，容器内端口 `6379`，宿主机映射 `16379`
- `app`：MindBridge FastAPI 服务，宿主机端口 `8080`

默认配置会让应用容器访问宿主机 Ollama：

```bash
cp .env.example .env
docker compose up -d --build
```

Windows PowerShell 也可以使用：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

本地通过 `http://127.0.0.1:8080` 访问时，`.env` 应配置
`AUTH_SECURE_COOKIE=false`。部署到具有 HTTPS 的线上域名时，必须改为
`AUTH_SECURE_COOKIE=true`，并将 `JWT_SECRET_KEY` 替换为独立生成的至少
32 字节随机密钥。不要把真实密钥、SMTP 授权码或 `.env` 提交到 Git。

如果 Ollama 已经有下列模型，容器即可使用真实本地聊天模型链路：

```text
mindbridge-qwen2.5-7b-ft:latest
```

## Chroma 向量库与快照

应用启动不再解析知识文件。内置 Markdown 与 `app/knowledge/pdf/` 下的 PDF 通过幂等异步命令提交；文件哈希未变化时不会重复解析或重复调用 Vision：

```bash
python -m app.cli.knowledge sync-builtins
```

知识库默认优先使用 Chroma 持久化向量库，embedding 由 OpenAI `text-embedding-3-small` 提供。查询时会同时取向量候选和 BM25 候选，按配置权重融合后进入本地 reranker。没有 `OPENAI_API_KEY`、缺少 `chromadb` 或向量调用失败时，会回退到本地 BM25 + `hybrid_score` reranker：

```env
OPENAI_API_KEY=你的_API_Key
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RERANK_ENABLED=true
CHROMA_PERSIST_DIR=data/chroma
CHROMA_SNAPSHOT_DIR=data/chroma-snapshots
```

管理员接口使用 Cookie 登录与 CSRF 防护。先登录并保存 Cookie：

```bash
curl -c admin.cookies -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  http://127.0.0.1:8080/api/auth/login
ADMIN_CSRF=$(awk '$6 == "mindbridge_csrf" {print $7}' admin.cookies)
curl -b admin.cookies http://127.0.0.1:8080/api/admin/knowledge/status
curl -b admin.cookies -H "X-CSRF-Token: $ADMIN_CSRF" -X POST \
  http://127.0.0.1:8080/api/admin/knowledge/rebuild-vector
curl -b admin.cookies -H "X-CSRF-Token: $ADMIN_CSRF" -X POST \
  http://127.0.0.1:8080/api/admin/knowledge/backup
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，如果 Chroma 或 embedding 服务不可用，系统会降级到本地 BM25 + 词面 rerank；设为 `true` 则启动或检索失败时直接暴露错误。

## 工具队列、限流与死信

心理报告生成后，工具链不会阻塞学生端流式回复，而是在业务事务内写入 Transactional Outbox：

```text
EXCEL_REPORT
CASE_CREATE -> ALERT_SEND
```

Outbox 发布到 RabbitMQ 后由 Celery worker 消费。事件消费、Excel 写入和个案创建保持幂等；预警发送有每分钟限流。任务按指数退避重试，超过 `TOOL_TASK_MAX_ATTEMPTS` 后进入 `dead_letter_records`，后台可查看死信、认领个案并追加处置备注。

```env
TOOL_TASK_MAX_ATTEMPTS=5
ALERT_EMAIL_RATE_LIMIT_PER_MINUTE=30
ALERT_EMAIL_DELIVERY_MODE=log
```

`ALERT_EMAIL_DELIVERY_MODE=log` 适合本地演示；生产发邮件时改为 `smtp` 并配置 SMTP。

## 邮件预警配置

高风险消息会触发心理报告，并在同一数据库事务内写入 Outbox 事件；发布器将事件投递到 RabbitMQ，再由 Celery worker 完成 Excel 台账、个案创建和邮件预警。发送邮件前需要在 `.env` 中配置 SMTP：

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-account@example.com
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
SMTP_USE_SSL=false
ALERT_EMAIL_FROM=your-account@example.com
ALERT_EMAIL_TO=counselor@example.com,admin@example.com
ALERT_EMAIL_SUBJECT_PREFIX=[MindBridge 高风险预警]
```

未配置 SMTP 或收件人时，系统不会中断聊天流程，但会在 `alert_records` 中写入 `FAILED` 记录，提示缺少的配置项。

## 接入本地微调 GGUF 模型

Python 版默认预留本地模型名：

```text
mindbridge-qwen2.5-7b-ft:latest
```

模型目录：

```text
models/mindbridge-qwen2.5-7b-ft/
```

需要放入的 GGUF 权重：

```text
models/mindbridge-qwen2.5-7b-ft/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf
```

如果本机已经有其他位置的 GGUF 模型文件，可以通过 `UPSTREAM_GGUF` 指定路径并建立软链接：

```bash
UPSTREAM_GGUF=/path/to/mindbridge-qwen2.5-7b-ft-q4_k_m.gguf ./scripts/create-finetuned-model.sh
```

创建 Ollama 模型：

```bash
./scripts/create-finetuned-model.sh
```

启动 Ollama：

```bash
./scripts/start-ollama.sh
```

启动 Python 服务：

```bash
AI_PROVIDER=ollama ./scripts/run-dev.sh
```

查看模型接入状态：

```bash
curl -b student.cookies http://127.0.0.1:8080/api/agent/status
```

这里的 `student.cookies` 由下方“调用示例”中的学生登录命令生成。

返回结果中的 `finetunedModel.ggufExists` 和 `finetunedModel.modelfileExists` 会显示模型资产是否就绪。
同时 `agentFramework.active` 会显示当前实际使用的 Agent 编排框架：

```text
event_driven_multi_agent
```

## 接入 OpenAI-compatible API

```bash
AI_PROVIDER=openai \
OPENAI_API_KEY=你的_API_Key \
OPENAI_MODEL=gpt-4o-mini \
OPENAI_EMBEDDING_MODEL=text-embedding-3-small \
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

知识库向量检索也使用同一个 `OPENAI_API_KEY` 调用 embeddings API。相关配置：

```env
KNOWLEDGE_VECTOR_ENABLED=true
KNOWLEDGE_VECTOR_REQUIRED=false
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_CANDIDATE_K=16
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
KNOWLEDGE_HYBRID_BM25_WEIGHT=0.35
KNOWLEDGE_RERANK_ENABLED=true
CHROMA_PERSIST_DIR=data/chroma
CHROMA_COLLECTION_NAME=mindbridge_knowledge
```

当 `KNOWLEDGE_VECTOR_REQUIRED=false` 时，缺少 API key 或 Chroma 不可用不会阻断聊天，系统会回退到本地 BM25 + `hybrid_score` reranker。若交付验收要求必须走 Chroma 向量检索，可设置 `KNOWLEDGE_VECTOR_REQUIRED=true`。

## 调用示例

学生流式聊天：

```bash
curl -c student.cookies -H 'Content-Type: application/json' \
  -d '{"username":"student","password":"student123"}' \
  http://127.0.0.1:8080/api/auth/login
CSRF=$(awk '$6 == "mindbridge_csrf" {print $7}' student.cookies)
curl -N -b student.cookies -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"message":"我最近很焦虑，晚上总是睡不着"}' \
  http://127.0.0.1:8080/api/chat/stream
```

高风险示例，会触发心理报告、风险个案创建和预警工具计划；Excel 保留为台账输出，邮件/log 是预警通道之一：

```bash
curl -N -b student.cookies -H "X-CSRF-Token: $CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"message":"我不想活了，感觉撑不下去了"}' \
  http://127.0.0.1:8080/api/chat/stream
```

管理员查看报告：

```bash
curl -c admin.cookies -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  http://127.0.0.1:8080/api/auth/login
curl -b admin.cookies http://127.0.0.1:8080/api/admin/reports
```

管理员追加知识库：

```bash
ADMIN_CSRF=$(awk '$6 == "mindbridge_csrf" {print $7}' admin.cookies)
curl -b admin.cookies -H "X-CSRF-Token: $ADMIN_CSRF" \
  -H 'Content-Type: application/json' \
  -d '{"source":"sleep-guide","content":"失眠时可先固定起床时间，减少睡前屏幕刺激，必要时联系校心理中心。"}' \
  http://127.0.0.1:8080/api/admin/knowledge
```

追加知识库返回 `202 Accepted`，解析、OCR、Vision、切块和索引均由 `mindbridge.ingestion` 队列异步完成。新版本完成索引集合校验后才原子激活；失败时旧 active 版本继续提供检索。

## RAG 评测

```bash
AI_PROVIDER=mock python -m app.rag_eval.runner
```

评测报告输出到：

```text
target/rag-eval-report.json
```

## 生产级 RAG 文档摄取

PDF 首先由 LiteParse 2.11.1 提取原生文字、坐标、页面图像和复杂度证据；其内置 OCR 默认关闭，可由 `RAG_LITEPARSE_OCR_ENABLED` 显式控制。LiteParse 不是最终 OCR，也不决定页面是否调用云模型。独立的二维路由器分别选择文字策略 `NATIVE / PADDLE_OCR / HYBRID` 与结构策略 `LOCAL / VISION`：纯文本页保留本地低成本路径，扫描文字交给 PaddleOCR，双栏、表格、漫画和信息图仅在确有结构或视觉语义需求时调用 Vision。`RAG_OCR_ENABLED=false` 时低质量文字页直接升级到 Vision；`RAG_VISION_ENABLED=false` 时需要视觉理解的页面进入 `NEEDS_REVIEW`，不会静默降级。

所有证据经过本地严格 Schema 校验与融合，形成唯一的 Canonical Document JSON。本地结构路径会过滤显式页眉、页脚、页码，以及位于页面边缘的浏览器打印时间、URL 和页码；缺少章节路径时使用文档标题作为 Chunk 上下文，不写入“未命名章节”占位词。父块仅在一个章节被拆成多个子块时保存完整章节上下文，并按 `RAG_PARENT_TARGET_TOKENS` 软目标切分，且不超过可拆分边界内的 `RAG_PARENT_MAX_TOKENS`；只有一个子块的短内容不再额外保存重复父块。子块按 target/min/max 参数切分，小尾块在不超过 max 时合并，相邻叙事子块最多复用 `RAG_CHILD_OVERLAP_TOKENS` 的上下文。表格继续按完整行切分并重复表头，不使用叙事文本重叠。检索命中带父块的子块后返回父块内容，同时保留 `documentId`、页码、章节路径和 block 引用。旧的固定字符窗口只保留为历史数据兼容路径，不再承担新文件摄取。

安装与启动：

```bash
pip install -r requirements.txt
pip install -r requirements-ingestion.txt
docker compose up -d --build worker-ingestion app
python -m app.cli.knowledge sync-builtins
```

默认 Vision 使用 OpenAI-compatible `/v1/chat/completions`，本项目配置为 `gpt-5.6-luna`；`RAG_VISION_BASE_URL` 和 `RAG_VISION_API_KEY` 留空时复用 `OPENAI_BASE_URL` 与 `OPENAI_API_KEY`。PaddleOCR 仅安装和加载在单并发 ingestion worker 中，Web 镜像不包含 Paddle 依赖。

隐私边界：`BUILTIN_PUBLIC` 可允许 Vision；管理员私有文件默认禁止云发送，上传时只有显式传入 `cloudVisionAllowed=true` 才会授权。学生聊天、心理报告、长期记忆和用户个人材料不得进入知识摄取 Vision Provider。原始文件、页面证据和 Canonical JSON 保存在 `RAG_ARTIFACT_DIR`，日志不记录整页 OCR/Vision 正文和 API Key。

管理员 API：

```text
POST /api/admin/knowledge/files
GET  /api/admin/knowledge/documents
GET  /api/admin/knowledge/documents/{documentId}
GET  /api/admin/knowledge/documents/{documentId}/pages
GET  /api/admin/knowledge/jobs/{jobId}
POST /api/admin/knowledge/jobs/{jobId}/retry
```

失败任务会返回脱敏的 `stage`、`error.code` 与 `error.retryable`。只有可重试失败可以直接重试；`NEEDS_REVIEW` 必须在人工确认后显式授权 Vision。回滚无需修改旧数据：失败版本保持 inactive，旧 active 版本继续服务。

五份 PDF 的评测集覆盖 270 页，另有 30 页深度条目：

```bash
python -m app.rag_ingestion.evaluation.runner --validate-only
python -m app.rag_ingestion.evaluation.runner --predictions target/rag-predictions.json
```

当前仓库中的页级标签是机器种子标注，CLI 会在人工复核前阻止其被当作发布金标。真实 Paddle/Vision smoke 需显式设置 `RAG_OCR_ONLINE_TEST=true` 与 `RAG_VISION_ONLINE_TEST=true`，避免普通离线测试产生网络调用或费用。

## 风险安全门评测

```bash
python -m app.risk_eval.runner
```

评测集覆盖直接意图、行动计划、隐性绝望、第三方求助、明确否认、研究/新闻语境和英文表达。报告输出到 `target/risk-eval-report.json`。这里的指标用于工程回归，不代表临床有效性。

## LoRA 训练复现

`training/` 提供去重、固定随机种子、标签分层切分、LoRA 训练和独立 test split 评测脚本。当前 2400 条数据会切为 1920/240/240，并在 `data/lora/splits/manifest.json` 记录源文件和各 split 的 SHA-256。完整命令与限制见 `training/README.md`。

## 单元测试

当前 `tests/` 里的基础回归用例使用 Python 标准库 `unittest`，不依赖 `pytest`：

```bash
python -m unittest discover -s tests
```

## Agent Runtime Harness

线上对话通过 `MindBridgeAgentHarness` 组织一次 Agent run。Harness 不改变事件驱动 runtime 内部的多 Agent 协作方式，而是在外层统一管理：

- 输入脱敏和 session 解析。
- Agent runtime 调用和多 Agent 协作结果接入。
- 心理报告落库和工具计划生成。
- 学生与助手消息持久化。
- Agent steps、知识召回、风险结果等 trace 数据输出。

因此 HTTP 层只负责认证和 SSE 流式输出，Agent 后处理逻辑集中在 runtime harness 内。

## Engineering Harness

项目提供一键工程 harness，用 mock AI、临时 SQLite、内存短期记忆和本地输出验证核心链路：

- Risk Safety Harness：高风险识别、报告生成、后台元数据不外显、事务 Outbox 事件生成。
- Agent Routing Harness：通过 `MindBridgeAgentHarness` 验证 CHAT / CONSULT 与独立风险维度的路由和多 Agent 步骤。
- Standard Skills Harness：验证 `skills/*/SKILL.md` 标准 Skill 加载、选择逻辑和交接摘要模板渲染。
- Structured Memory Harness：验证结构化摘要触发阈值、MySQL 检查点、Redis 缓存、消息水位线、最近原文窗口和确定性降级。
- RAG Harness：基于内置评测集验证 Recall@K、MRR、NDCG 和 HitRate。
- API Harness：健康检查、认证授权、SSE 聊天、管理员知识库接口。

```bash
python3 -m app.harness.runner
```

报告输出到：

```text
target/harness/harness-report.json
target/harness/rag-eval-report.json
```

## MCP 工具服务

MCP Python 包建议使用 Python 3.10 或 3.11 安装运行。

```bash
python -m app.mcp_tools.server
```

业务后端触发报告后处理时，通过事务 Outbox、RabbitMQ 与 Celery worker 执行。MCP server 可独立启动；其 Excel、个案和预警命令也必须进入同一个工具治理与 Outbox 边界，不再绕过生产可靠性链路直接产生副作用。

暴露工具：

- `mindbridge_excel_report`
- `mindbridge_case_create`
- `mindbridge_alert_send`
- `mindbridge_alert_ack`
- `mindbridge_case_note_add`
- `mindbridge_alert_notify`

内置标准 Skills 位于 `skills/*/SKILL.md`，运行时由 `MindBridgeSkillRegistry` 加载：

- `supportive_response_baseline`：心理咨询与风险回复的基础共情、边界和学生端表达规则。
- `high_risk_safety_plan`：高风险时引导模型优先完成短期安全计划。
- `anxiety_grounding_support`：焦虑、惊恐、崩溃场景的稳定化和 grounding 指引。
- `sleep_routine_support`：失眠、睡眠节律紊乱场景的安全睡眠建议。
- `academic_stress_planning`：考试、作业、论文、绩点压力的下一步拆解。
- `referral_resource_guidance`：校内心理中心、辅导员、可信任支持人和紧急资源转介。
- `counselor_handoff_summary`：生成给辅导员/管理员看的个案交接摘要模板。
