# MindBridge 生产级上下文、Prompt、记忆与错误恢复设计

## 1. 背景与目标

MindBridge 已经具备事件驱动多 Agent、MySQL 会话记录、Redis 短期记忆、结构化会话摘要、长期记忆、RAG、Skill、安全审查与 Transactional Outbox。现有实现的主要问题不是缺少功能，而是四类基础能力没有形成统一闭环：

- 上下文压缩按消息条数和字符数处理，不理解模型的真实 token 预算；
- Prompt 分散在 Agent 和 Service 中，缺少版本、稳定前缀、加载原因和回归治理；
- 模型调用只返回文本，无法统一处理 finish reason、token usage、Retry-After、流式中断和模型切换；
- 长期记忆具备提取和选择，但缺少证据、置信度、冲突消解、状态演进和低频巩固。

本设计吸收 `learn-claude-code/s08_context_compact` 至 `s11_error_recovery` 的核心思想，但不复制其教学实现。目标是在不重写现有业务 Runtime 的前提下，建立可预算、可解释、可恢复、可灰度的生产级基础设施。

## 2. 已确认决策

- 保留现有 FastAPI、SQLAlchemy、MySQL、Redis、RabbitMQ、Celery、事件驱动 Coordinator 和安全审查流程。
- Ollama 是主模型 Provider，OpenAI-compatible 云模型是降级 Provider。
- `LOW`、`MEDIUM` 请求仅可将脱敏、最小必要上下文发送至云端。
- `HIGH` 请求禁止自动出域；本地模型不可用时使用确定性安全回复和人工求助引导。
- 允许新增 Python 依赖、配置、数据表和 Alembic 迁移。
- 保持现有 HTTP API、SSE 事件和前端行为向后兼容。
- 所有新增行为采用测试驱动开发，并通过 feature flag、shadow 记录和灰度切换上线。

## 3. 非目标

- 不重写事件驱动多 Agent 协议和黑板模型。
- 不引入新的向量数据库或替换现有 RAG 摄取链路。
- 不允许管理员在数据库中直接编辑核心安全 Prompt。
- 不把完整 Prompt、未脱敏用户输入或高风险原文写入新增 trace 表。
- 不追求对所有未知模型进行精确 tokenizer 推断；未知模型必须采用保守估算并扩大安全余量。

## 4. 总体架构

```text
Agent / Memory Worker
        │
        ▼
PromptRegistry + PromptAssembler
        │  PromptManifest + ContextEnvelope
        ▼
ContextPlanner + CompactionEngine
        │  ContextPlan
        ▼
RecoveryOrchestrator
        │
        ▼
ModelGateway
        ├─ OllamaProvider
        └─ OpenAICompatibleProvider

MemoryService V2 ── 会话摘要、长期记忆、证据和巩固
Trace/Evaluation ── Prompt、预算、压缩、恢复和质量指标
```

`ModelGateway` 是唯一模型出口。`RecoveryOrchestrator` 只处理单次模型调用生命周期，Coordinator 继续处理 Agent 任务级失败和业务 fallback。两层职责不得混用。

## 5. 核心数据契约

### 5.1 模型调用

```python
@dataclass(frozen=True)
class ModelRequest:
    request_id: str
    agent_name: str
    task_name: str
    risk_level: RiskLevel
    messages: tuple[AiMessage, ...]
    preferred_model: str
    max_output_tokens: int
    stream: bool = False
    output_schema_id: str = ""
    prompt_manifest_hash: str = ""
    cloud_egress_allowed: bool = False

@dataclass(frozen=True)
class ModelResult:
    text: str
    provider: str
    model: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    request_id: str
    provider_request_id: str = ""
    partial: bool = False
```

Provider 异常统一映射为 `ModelError`：

- `RATE_LIMITED`
- `OVERLOADED`
- `TIMEOUT`
- `NETWORK`
- `AUTHENTICATION`
- `PROMPT_TOO_LONG`
- `OUTPUT_TRUNCATED`
- `INVALID_RESPONSE`
- `STREAM_INTERRUPTED`
- `CONTENT_POLICY`
- `PERMANENT`

错误对象必须携带 `retryable`、`retry_after_seconds`、`provider`、`model`、`attempt` 和脱敏消息。

### 5.2 上下文

```python
@dataclass(frozen=True)
class ContextSection:
    id: str
    category: str
    content: str
    priority: int
    required: bool
    compressible: bool
    sensitivity: str
    provenance_ids: tuple[str, ...]
    estimated_tokens: int = 0

@dataclass(frozen=True)
class ContextEnvelope:
    request_id: str
    session_id: str
    agent_name: str
    risk_level: RiskLevel
    target_model: str
    sections: tuple[ContextSection, ...]

@dataclass(frozen=True)
class ContextPlan:
    sections: tuple[ContextSection, ...]
    input_budget: int
    tokens_before: int
    tokens_after: int
    actions: tuple[CompactionAction, ...]
    reactive: bool = False
```

### 5.3 Prompt

```python
@dataclass(frozen=True)
class PromptDefinition:
    id: str
    version: str
    template_path: str
    placement: str
    priority: int
    required: bool
    cache_scope: str
    sensitivity: str
    compress_policy: str
    allowed_agents: frozenset[str]

@dataclass(frozen=True)
class PromptManifest:
    release: str
    agent_name: str
    task_name: str
    mode: str
    model: str
    section_versions: tuple[tuple[str, str], ...]
    section_hashes: tuple[tuple[str, str], ...]
    section_tokens: tuple[tuple[str, int], ...]
    static_prefix_hash: str
    dynamic_context_hash: str
    total_tokens: int
    output_schema_id: str
```

Manifest 不保存 Prompt 原文，只保存版本、hash、token 和脱敏来源元数据。

## 6. 模型能力与 Token 计量

新增 `ModelCapabilitiesRegistry`，生产配置是权威来源：

- Provider；
- context window；
- 默认和最大输出 token；
- tokenizer 类型与路径；
- streaming、structured output、Retry-After 和显式缓存能力；
- 是否允许云端处理。

输入预算公式：

```text
input_budget
= context_window
- requested_output_tokens
- recovery_reserve
- provider_safety_margin
```

Token estimator 可插拔：

- OpenAI 已知模型使用 `tiktoken`；
- Qwen/Ollama 优先使用配置的 Hugging Face/Qwen tokenizer；
- tokenizer 不可用时使用保守 Unicode 估算；
- Ollama 的 `prompt_eval_count` 和 OpenAI usage 用于记录估算误差，后续请求采用配置化安全倍率；
- 未知模型不得因估算失败而跳过预算检查。

默认阈值：soft limit 为输入预算的 80%，hard limit 为 95%，压缩目标为 70%。所有比例允许按模型覆盖。

## 7. 上下文规划与分层压缩

### 7.1 优先级

P0 永不删除：平台身份、安全规则、Agent 职责、当前输入、HIGH 风险约束、输出 Schema。

P1 强保留：最近完整 turn、未解决事项、安全 artifact、强制 Skill、高相关 RAG。

P2 可压缩：旧会话原文、长期记忆、可选 Skill、普通 RAG、私有 Agent 记忆。

P3 优先移除：重复说明、低相关检索结果、过期私有记忆、调试字段和可按 ID 回读的冗长内容。

### 7.2 压缩管线

1. L0 规范化：稳定排序、空内容清理、role 校验、token 统计。
2. L1 无损去重：RAG 父块、长期记忆、Skill 和私有记忆按稳定 ID/hash 去重。
3. L2 预算选择：按优先级、相关性、置信度和 section 上限裁剪。
4. L3 摘要替换：使用结构化会话摘要替代旧原文，动态保留至少两个完整 turn。
5. L4 局部 LLM 压缩：分别压缩 RAG、长期记忆或旧对话，必须保留来源 ID 并通过 Schema 与隐私校验。
6. L5 Reactive Emergency Plan：只保留 P0、最近一轮、结构化摘要、一条强制 Skill 和一条最高相关 RAG，最多执行一次。

异步会话摘要落后且达到 hard limit 时，调用 `ConversationSummaryService.ensure_through()` 在行锁和消息水位线保护下同步推进，不建立第二套摘要系统。

任何调用前必须满足 `estimated_tokens <= hard_limit`。当前输入本身超限时返回 `INPUT_TOO_LARGE`，并提供拆分建议；风险规则仍对完整脱敏文本执行。

## 8. Prompt Registry 与组装

Prompt 文件按 `global/`、`agents/`、`tasks/` 和 `schemas/` 管理。核心安全 Prompt 只随代码发布。

稳定顺序：

1. Global Static Prefix；
2. Agent Static Contract；
3. Task/Mode Instructions；
4. Dynamic Context Policy；
5. Context Attachments；
6. 历史对话；
7. 当前用户输入。

RAG、长期记忆、摘要和用户资料均为 `UNTRUSTED_DATA`，不得作为指令执行。动态内容不得进入全局缓存块。

每个整体发布使用 `mindbridge-prompt-release` 标识，支持新旧 release 并存、灰度和回滚。Prompt 内容变化而版本未提升、输出 Schema 与解析器不匹配、敏感动态内容进入静态前缀时，CI 必须失败。

缓存分三层：模板编译缓存、静态 section 缓存、Provider 稳定前缀或显式 prompt cache。

## 9. ModelGateway

### 9.1 Provider Adapter

`OllamaProvider` 和 `OpenAICompatibleProvider` 实现同一协议：

```python
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResult: ...
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...
```

Provider 负责 HTTP payload、finish reason、usage、request ID 和原始异常到标准错误的映射，不负责业务重试。

### 9.2 云端出域守卫

`CloudEgressPolicy` 在路由前检查：

- HIGH 永远拒绝云路由；
- LOW/MEDIUM 必须已通过 `PrivacySanitizer`；
- 只允许发送 Context Plan 中被选择的最小 section；
- Prompt Manifest 记录出域 section ID、token 和 hash，不记录原文；
- 守卫失败返回 `CLOUD_EGRESS_DENIED`，不能被重试绕过。

## 10. RecoveryOrchestrator

### 10.1 恢复状态

```python
@dataclass
class RecoveryState:
    attempt: int = 0
    transient_retries: int = 0
    output_escalations: int = 0
    continuations: int = 0
    reactive_compactions: int = 0
    consecutive_overloads: int = 0
    stream_resumes: int = 0
    current_route: str = "ollama"
```

每类错误拥有独立预算，不使用一个通用 `max_attempts` 混合计算。

### 10.2 状态转换

```text
SUCCESS
  └─ 返回 ModelResult

OUTPUT_TRUNCATED
  ├─ 首次：在模型能力上限内提升 max_output_tokens，原请求重试
  ├─ 再次：保存已审查部分并注入 continuation，最多 2 次
  └─ 收益递减：停止续写并返回 PARTIAL

PROMPT_TOO_LONG
  ├─ reactive compact 尚未执行：重新规划并重试
  └─ 已执行：CONTEXT_UNRECOVERABLE

RATE_LIMITED / OVERLOADED / NETWORK / TIMEOUT
  ├─ Retry-After 优先
  ├─ 否则指数退避 + 0~25% jitter
  ├─ 连续过载达到阈值：评估云降级
  └─ 重试预算耗尽：业务 fallback

STREAM_INTERRUPTED
  ├─ 尚未向客户端释放：相同请求安全重试
  ├─ 已释放低风险片段：生成 replace 事件或有限续写
  └─ HIGH：不流式释放，直接走完整安全审查

INVALID_RESPONSE
  ├─ 结构化任务：附校验错误重试一次
  └─ 再失败：确定性解析或业务 fallback
```

默认退避为 `min(0.5 * 2^(attempt-1), 32s) + jitter`，优先尊重 Retry-After。恢复总时间受调用 deadline 约束，不能因为十次重试突破 Agent task timeout。

### 10.3 模型降级

- LOW/MEDIUM：Ollama 连续过载、网络不可用或超时达到阈值后，可切换 OpenAI-compatible；切换前重新执行隐私守卫和云模型预算规划。
- HIGH：禁止切换云端；本地失败后发布 `high_risk_local_fallback`。
- prompt too long 不触发模型切换，先重新规划上下文。
- authentication、配置错误和永久错误不重试。
- fallback model 切换产生显式 trace event。

### 10.4 输出截断与收益递减

Provider 必须返回 finish reason。首次 `max_tokens` 不把截断文本追加到历史，提升输出预算后重试相同请求。达到模型输出上限后，才保存已审查文本并注入续写指令。

连续两次 continuation 新增少于配置阈值或重复度过高时停止，避免无效循环。最终状态为 `PARTIAL`，而不是伪装成完整成功。

### 10.5 Agent 异常边界

Agent 不再使用宽泛 `except Exception` 吞掉模型错误。只有以下情况允许 Agent 内部降级：

- 确定性业务规则可安全替代模型结果；
- 降级结果显式携带 `generationStatus`、失败类型和 route；
- 恢复层已经耗尽调用级策略，或错误明确不可重试。

Coordinator 保留 task timeout、Agent 隔离和 fallback artifact，但不再重复执行 Provider 重试。

## 11. MemoryService V2

### 11.1 会话摘要

保留现有 `conversation_memory_summaries` 为权威检查点，升级 Schema 版本并修复确定性摘要截断后新信息无法进入的问题。

新增：

- `scheduled_through_message_id`，防止 worker 延迟时重复调度；
- compare-and-set 水位线；
-同步 `ensure_through()` 紧急推进；
-摘要内容的 section token 上限；
-摘要生成 Prompt release、Context Plan hash 和实际 token usage。

### 11.2 长期记忆实体

长期记忆新增：

- `status`: `ACTIVE`、`SUPERSEDED`、`REJECTED`、`EXPIRED`；
- `confidence`；
- `evidence_message_ids_json`；
- `extraction_method`、`model_provider`、`model_name`、`prompt_version`；
- `supersedes_memory_id`；
- `confirmation_count`、`usage_count`；
- `last_confirmed_at`、`expires_at`。

写入必须有证据消息 ID。助手推测、一次性情绪、诊断、高风险原文和敏感身份信息继续禁止保存。

### 11.3 冲突和巩固

Memory Consolidator 通过 Outbox/Celery 低频运行，具备四个门：

- 距上次巩固达到最小时间；
- ACTIVE 记忆数量或新增数量达到阈值；
- 足够多的新会话参与；
- 同一用户不存在进行中的 consolidation lock。

巩固不会物理删除记忆，而是：

- 合并重复证据；
- 新记忆 supersede 旧记忆；
- 矛盾无法判断时同时保留并降置信度；
- 长期未确认且未使用的上下文记忆标记 EXPIRED；
- PROFILE 和明确 PREFERENCE 不因时间单独过期。

用户删除记忆仍执行真实删除，并清理相关缓存和索引。

### 11.4 选择策略

候选评分由以下因素组成：

```text
语义/关键词相关性
+ 置信度
+ 最近确认度
+ 使用反馈
- 过期惩罚
- 冲突惩罚
```

先读取轻量索引，再加载最多配置数量的 ACTIVE 正文。被选择的记忆更新 `usage_count`，但不因为单纯使用而提升事实置信度。

## 12. 数据库与迁移

新增表：

### `model_call_traces`

保存 request、Agent、task、risk、provider/model、finish reason、token、latency、attempt、route、error code、Prompt Manifest hash 和 Context Plan hash。禁止保存 Prompt 原文。

### `context_compaction_records`

保存压缩前后 token、触发原因、执行层级、action 元数据、摘要水位线和结果状态。

### `memory_consolidation_runs`

保存用户、输入记忆数量、合并/supersede/expire 数量、状态、锁和脱敏错误。

修改：

- `conversation_memory_summaries` 增加调度水位线和预算/Prompt 元数据；
- `long_term_memories` 增加状态、证据、置信度、版本、确认、使用、冲突和过期字段。

迁移必须兼容已有数据：旧长期记忆迁移为 `ACTIVE`、中等置信度、`extraction_method=legacy`，证据为空；首次重新确认后补齐证据。

## 13. 可观测性

核心指标：

- `model_call_total{provider,model,status}`；
- `model_call_latency_ms`；
- `model_retry_total{reason}`；
- `model_fallback_total{from,to,risk}`；
- `context_tokens_before/after`；
- `context_compaction_total{layer,reason}`；
- `prompt_tokens{agent,section}`；
- `prompt_release_total{release}`；
- `memory_extract_total{status}`；
- `memory_consolidation_total{status}`；
- `cloud_egress_total{risk,decision}`。

Trace 中的历史 artifact payload 必须经过独立 redactor，避免当前 `AgentTraceService` 将完整记忆正文和历史消息写入 `agent_steps_json`。

## 14. 灰度与回滚

新增开关：

- `MODEL_GATEWAY_ENABLED`
- `PROMPT_REGISTRY_ENABLED`
- `CONTEXT_PLANNER_ENABLED`
- `RECOVERY_ORCHESTRATOR_ENABLED`
- `MEMORY_V2_ENABLED`
- `MEMORY_CONSOLIDATION_ENABLED`
- `CONTEXT_PLANNER_SHADOW_MODE`

上线顺序：

1. Shadow 记录 Context Plan，不改变实际 Prompt；
2. Prompt Registry 仅迁移内部分类和摘要任务；
3. ModelGateway 接管非流式 LOW 请求；
4. ContextPlanner 接管 LOW/MEDIUM；
5. Recovery 接管流式请求；
6. Memory V2 双写并校验；
7. HIGH 请求最后切换，且始终保持本地和确定性 fallback。

每个阶段可通过 feature flag 回到旧路径。数据库迁移只增加字段和表，回滚应用版本时不会破坏旧代码读取。

## 15. 测试与评测

### 单元测试

- Provider 错误映射、Retry-After、finish reason 和 usage；
- Prompt section 顺序、版本、hash、严格变量和缓存；
- token 预算、优先级、不变量和每层压缩；
- HIGH 云出域拒绝；
-恢复状态转换和收益递减；
-长期记忆证据、supersede、冲突和巩固门控。

### 集成测试

- Ollama 失败后 LOW/MEDIUM 切云；
- HIGH 本地失败后确定性安全回复且云 Provider 零调用；
- prompt too long 只 reactive compact 一次；
-流式中断不重复或泄漏未审查内容；
-异步摘要与同步 ensure_through 并发时水位线单调；
-旧数据库迁移后 API 行为兼容。

### 长上下文评测

构造短消息、超长单消息、长会话、多 RAG、多 Skill、冲突记忆和摘要滞后数据集。至少记录：

- context overflow 恢复率；
-关键信息保留率；
-安全规则保留率；
-平均输入 token 降幅；
-额外模型调用成本；
-P50/P95 延迟；
-云降级比例。

### 混沌测试

注入 429、529、超时、连接断开、无效 JSON、流中断、Redis 不可用、摘要 worker 延迟和数据库短暂失败。

## 16. 验收标准

- 所有模型调用均经过 ModelGateway，业务代码不存在新增直接 Provider HTTP 调用。
- 所有 Prompt 均可关联 Prompt release、section 版本和 Manifest hash。
- 每次模型调用前存在可验证的 token 预算结果。
- HIGH 请求在任何失败路径下云 Provider 调用次数为零。
- prompt too long 最多执行一次 reactive compact，不存在无限循环。
- 429/529/timeout 遵守独立重试预算、deadline、Retry-After 和 jitter。
- max output tokens 可被识别，并通过升级、有限续写或 PARTIAL 明确处理。
- 长期记忆新写入具有证据、置信度和状态；冲突不通过覆盖或物理删除静默丢失。
- 新增 trace 不保存 Prompt 原文、未脱敏输入或高风险原话。
- 现有 API、SSE、前端和安全审查测试保持兼容。
- 全部新增专项测试通过；除已知缺失本地 PDF 数据的基线测试外，现有自动化测试通过。

## 17. 简历与面试表达

最终项目点不描述为“照搬 Claude Code”，而应表述为：

> 针对本地大模型多 Agent 心理陪伴系统，设计并实现统一 Model Gateway、预算驱动 Context Planner、版本化 Prompt Registry、分层错误恢复状态机和证据化长期记忆；在 HIGH 风险数据禁止出域的约束下，实现 Ollama 主路由与云模型受控降级，并通过长上下文评测、混沌测试和可审计 trace 验证稳定性。

