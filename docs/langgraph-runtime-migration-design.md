# MindBridge 自研 Agent Runtime 迁移 LangGraph 设计说明

状态：已实施
目标读者：负责 MindBridge Runtime 改造的 Coding Agent
范围：仅替换多 Agent 编排与运行时基础设施，不重写现有领域服务

> 最终实施决策（2026-08-17）：`0817_langgraph-runtime-migration` 分支只保留 LangGraph Runtime，不在该分支内保留旧 Coordinator、Blackboard、AgentTask、AgentRegistry 或 custom Runtime 回滚开关。需要回退时直接切换到迁移前分支。本文中关于双 Runtime Feature Flag、灰度混跑和旧 Runtime 代码保留的内容属于早期迁移方案，不再适用于最终实现。

## 1. 决策摘要

MindBridge 使用 LangGraph 替换当前自研的多 Agent 调度层。

本次改造保留 Agent Harness、Agent 领域能力、Artifact 契约、Model Gateway、上下文治理、Memory、RAG、安全规则、MySQL 业务事务、Transactional Outbox、RabbitMQ/Celery 和本地隐私 Trace。

本次改造替换 CollaborationBlackboard 的具体实现、EventDrivenCoordinator、AgentTask/TaskStatus、能力认领、手写 asyncio 并行调度、通用 Agent 重试以及纯调度 Event。

共享 Blackboard 的设计思想不删除，而是使用 LangGraph Typed State 重新实现：

~~~text
当前：
Agent -> CollaborationBlackboard -> Agent

改造后：
LangGraph Node -> AgentState -> LangGraph Node
~~~

LangGraph 负责通用调度机制，MindBridge 继续负责领域安全策略。

## 2. 改造目标

### 2.1 功能目标

1. 使用 StateGraph 表达单轮对话执行图。
2. 使用 Typed State 替换内存 Blackboard。
3. 使用 Graph 节点和条件边替换 Coordinator 的缺失 Artifact 扫描。
4. 使用 LangGraph 节点并行替换 asyncio.TaskGroup 调度。
5. 使用 RetryPolicy、错误分支和 Checkpoint 替换通用任务重试与恢复代码。
6. 保留所有现有安全不变量和业务副作用语义。
7. 保持 AgentHarness、AgentRunResult 和 API 层接口尽量稳定。
8. 支持通过 Feature Flag 在旧 Runtime 与 LangGraph Runtime 间切换。
9. 为生产环境提供可持久化 Checkpointer 接口。
10. 将 LangGraph Trace、现有 MySQL 领域 Trace 和 Prometheus 指标关联起来。

### 2.2 非目标

本次改造不包含：

1. 重写 Model Gateway。
2. 重写 RAG 摄取和检索链路。
3. 重写长期记忆或 Memory Dream。
4. 替换 MySQL、Redis、RabbitMQ 或 Celery。
5. 将业务 Outbox 改造成 LangGraph Checkpoint。
6. 将全部心理数据发送到 LangSmith。
7. 在第一阶段引入多个同能力 Agent 的竞标机制。
8. 改变现有 HTTP API 和前端交互协议，除非为恢复能力增加可选字段。

## 3. 当前架构

当前单轮执行链路：

~~~text
FastAPI / SSE
    |
    v
MindBridgeAgentHarness
    |
    v
EventDrivenAgentRuntimeService
    |
    v
CollaborationBlackboard
    |
    v
EventDrivenCoordinator
    |
    +--> AgentRegistry
    +--> AgentTask / TaskStatus
    +--> AgentDecision.claim()
    +--> asyncio.TaskGroup
    +--> Runtime retry / fallback
    |
    v
UnderstandingAgent / SafetyAgent / ContextAgent / ResponseAgent
    |
    v
AgentRunResult
    |
    v
Harness MySQL transaction
    |
    +--> Message / Report / Trace
    +--> Transactional Outbox
    |
    v
RabbitMQ / Celery
~~~

当前 Runtime 已经具备：

- 单轮共享 Blackboard。
- Task、Event、Message、Artifact 协作协议。
- 能力匹配与认领。
- 同轮并发执行。
- 单任务超时和有限重试。
- 上下文超限后的 Reactive Compact。
- Agent 失败后的保守 Artifact。
- 独立 Output Safety。
- 最终结果验收。

当前主要限制：

- Blackboard 只存在于当前 Python 进程。
- 进程中途失败后不能从某个 Agent 步骤继续。
- Task 生命周期和并发合并逻辑需要自行维护。
- AgentStep 与原始 Event 存在重复 Trace。
- 当前 Task 到 Agent 基本固定，能力竞标收益有限。
- 新增复杂分支、人工审核和长任务恢复时维护成本较高。

## 4. 目标架构

~~~text
FastAPI / SSE
        |
        v
MindBridgeAgentHarness
        |
        v
LangGraphAgentRuntime
        |
        v
StateGraph + AgentState + Checkpointer
        |
        +--> prefetch_memory
        +--> understand_intent
        +--> assess_safety
        +--> gather_context
        +--> generate_response
        +--> review_response
        +--> compact_context
        +--> safe_fallback
        +--> finalize
        |
        v
Final AgentState
        |
        v
state_to_agent_run_result()
        |
        v
MindBridgeAgentHarness
        |
        +--> MySQL business transaction
        +--> Transactional Outbox
        +--> Redis best-effort cache
        |
        v
RabbitMQ / Celery
~~~

职责边界：

| 层 | 负责内容 |
|---|---|
| FastAPI / ChatService | HTTP、认证、SSE 生命周期、客户端投递 |
| AgentHarness | 输入脱敏、会话、业务事务、报告、Trace、Outbox、Redis 更新 |
| LangGraph Runtime | State、节点调度、并行、条件边、节点重试、Checkpoint |
| Agent Nodes | 意图、风险、上下文、回复和安全审核领域逻辑 |
| Model Gateway | Provider 路由、模型级重试、Deadline、流恢复、云外发策略 |
| Domain Services | Prompt、Context、Memory、RAG、安全规则 |
| Celery Workers | 报告、个案、告警、记忆和知识摄取等异步副作用 |

## 5. 组件替换边界

| 当前组件 | 迁移后的处理 |
|---|---|
| CollaborationBlackboard | 替换为 AgentState |
| EventDrivenCoordinator | 替换为 StateGraph、条件边和 finalize 节点 |
| AgentRegistry | 固定角色场景下删除；未来动态 Agent 使用 Router / Send |
| AgentDecision.claim() | 删除，改为明确节点路由 |
| AgentTask / TaskStatus | 删除，使用 LangGraph Node / Task 状态 |
| asyncio.TaskGroup | 替换为 LangGraph 并行分支 |
| Runtime 通用重试 | 替换为 RetryPolicy 和错误路由 |
| _derive_missing_work() | 替换为 Graph 边和条件路由 |
| _try_accept_final() | 替换为 finalize 节点 |
| _events_to_steps() | 替换为 Graph Trace 适配器 |
| AgentArtifact | 保留并规范 Schema |
| 领域安全 Event | 保留 |
| 纯调度 Event | 删除 |
| AgentHarness | 保留并修改 Runtime 调用 |
| AgentRunResult | 保留为 Runtime 对 Harness 的稳定契约 |
| Model Gateway | 完整保留 |
| Transactional Outbox | 完整保留 |

## 6. AgentState 设计

建议新增 app/graph/state.py：

~~~python
from operator import add
from typing import Annotated, TypedDict


class AgentState(TypedDict, total=False):
    # 执行关联
    turn_id: str
    user_id: int
    session_id: str

    # 仅保存脱敏输入，禁止保存原始输入
    model_input: str

    # 领域 Artifact 的当前版本
    memory: dict
    intent: dict
    risk: dict
    context: dict
    response_candidate: dict
    output_safety: dict

    # 追加式审计集合
    artifacts: Annotated[list[dict], add]
    domain_events: Annotated[list[dict], add]
    errors: Annotated[list[dict], add]

    # 流程控制
    revision_count: int
    reactive_compaction_used: bool
    status: str

    # 最终结果
    final_response: str
    final_artifact_id: str
~~~

State 设计要求：

1. 原始用户输入只能由 Harness 持有。
2. model_input 必须已经脱敏。
3. Checkpoint 中不得保存完整心理报告。
4. 长期记忆优先保存 ID 和受控摘要，不保存无界正文。
5. 并行节点写入不同的标量字段。
6. 多节点共同写入的列表必须配置 Reducer。
7. Artifact 必须使用 JSON 可序列化结构。
8. 不将 SQLAlchemy Session、Redis Client、模型 Client 写入 State。
9. 服务依赖通过 Runtime Context 或闭包注入节点。

## 7. Artifact 设计

Artifact 属于 MindBridge 领域契约，不能因迁移 LangGraph 而删除。

保留以下 Artifact：

- memory
- intent
- risk
- context
- response_candidate
- output_safety

建议统一结构：

~~~json
{
  "id": "risk:01...",
  "kind": "risk",
  "owner": "SafetyAgent",
  "payload": {
    "risk": "HIGH",
    "confidence": 0.97
  },
  "confidence": 0.97,
  "metadata": {
    "fallback": false
  }
}
~~~

State 中同时保存：

1. 当前有效 Artifact，例如 state["risk"]。
2. 追加式 artifacts 列表，用于领域审计。

新的候选回复必须生成新的 Artifact ID。Output Safety 必须记录被审核候选回复的 ID。

## 8. Event 设计

删除纯调度 Event：

- TASK_CREATED
- TASK_CLAIMED
- TASK_RELEASED
- TASK_CLOSED
- ROUND_STARTED
- BUDGET_EXHAUSTED

这些信息由 LangGraph 节点 Trace、Checkpoint 和任务状态提供。

保留领域 Event：

- SAFETY_OVERRIDE
- REVISION_REQUESTED
- FINAL_ACCEPTED
- FALLBACK_APPLIED
- CLOUD_EGRESS_BLOCKED
- CONTEXT_COMPACTED
- CONTEXT_UNRECOVERABLE

领域 Event 写入 state["domain_events"]，并在 Graph 完成后由 AgentTraceService 落入 MySQL。

不得同时保存一份 AgentStep 摘要和一份完全重复的原始调度 Event。

## 9. Graph 执行流程

~~~text
START
  |
  v
prefetch_memory
  |
  +-----------------------+
  |                       |
  v                       v
understand_intent     assess_safety
  |                       |
  +-----------+-----------+
              |
              v
         route_context
          /         \
         v           v
gather_context   skip_context
         \           /
          +----+----+
               |
               v
       generate_response
               |
               v
        review_response
        /      |       \
       v       v        v
   revise   approve   fallback
      |        |         |
      v        v         v
generate   finalize  safe_fallback
      ^                  |
      |                  v
      +-- 最多一次 -- finalize
                         |
                         v
                        END
~~~

### 9.1 Memory 前置

Understanding 和 Safety 必须等待 memory Artifact：

~~~text
prefetch_memory
→ understanding 与 safety 并行
~~~

原因是“是”“还是睡不着”等输入不能脱离历史上下文判断。

### 9.2 Context 路由

保持当前条件：

~~~python
needs_context = (
    intent == "CONSULT"
    or risk in {"MEDIUM", "HIGH"}
)
~~~

行为：

| Intent | Risk | Context |
|---|---|---|
| CHAT | LOW | 跳过完整 RAG |
| CONSULT | LOW | 加载 |
| CHAT / CONSULT | MEDIUM | 加载 |
| CHAT / CONSULT | HIGH | 加载并提高优先级 |

### 9.3 回复审核

所有 response_candidate 都必须经过独立 review_response 节点。

验收条件：

~~~python
response = state["response_candidate"]
review = state["output_safety"]

assert review["metadata"]["responseArtifactId"] == response["id"]
assert review["payload"]["status"] in {"APPROVED", "FALLBACK"}
~~~

REVISE 时最多重新生成一次。第二次仍不通过则进入 safe_fallback。

## 10. 节点契约

| 节点 | 输入 | 输出 | 失败降级 |
|---|---|---|---|
| prefetch_memory | user_id、session_id | memory | 空历史、无长期记忆 |
| understand_intent | model_input、memory | intent | 确定性 CHAT / CONSULT |
| assess_safety | model_input、memory | risk | 风险规则和 heuristic |
| gather_context | intent、risk、memory | context | 空 RAG、保留必要安全上下文 |
| generate_response | intent、risk、memory、context | response_candidate | 安全模板候选回复 |
| review_response | risk、response_candidate | output_safety | fail-closed 安全模板 |
| compact_context | context、错误 | 压缩 context | CONTEXT_UNRECOVERABLE |
| safe_fallback | risk | fallback response | 静态最低风险模板 |
| finalize | response、review | final_response | 拒绝非法验收 |

每个节点必须满足：

1. 输入输出 JSON 可序列化。
2. 不在节点内直接发送邮件、写 Excel 或创建外部副作用。
3. 外部读取允许失败降级。
4. 模型调用统一经过 Model Gateway。
5. 每个节点输出结构化 Artifact，不直接修改其他节点私有状态。

## 11. Agent 角色迁移

现有 Agent 领域逻辑保留，逐步改造成节点：

| 当前 Agent | 新节点 |
|---|---|
| ContextAgent 的记忆预取 | prefetch_memory |
| UnderstandingAgent | understand_intent |
| SafetyAgent 风险识别 | assess_safety |
| ContextAgent 上下文加载 | gather_context |
| ResponseAgent | generate_response |
| SafetyAgent 回复审核 | review_response |
| Coordinator 最终验收 | finalize |

第一阶段可以包装现有服务，避免一次性重写：

~~~python
async def understand_intent_node(state, runtime):
    service = runtime.context.services
    result = await service.understanding.classify(
        state["model_input"],
        state["memory"],
    )
    return result_to_state_update(result)
~~~

迁移完成后移除 decide()、claim()、AgentTask 等旧接口。

## 12. Router 与 Send

当前固定四 Agent 场景不需要重新实现复杂能力竞标。

优先使用静态边和条件边。

Router 用于：

- 根据 intent / risk 决定是否进入 Context。
- 根据审核结果进入 approve / revise / fallback。
- 根据 ModelError 决定 retry / compact / fallback。

Send 仅在需要动态 fan-out 时使用，例如：

- 同时调用 Vector、BM25 和 Memory Retriever。
- 动态调用多个专业 Agent。
- 根据文档页数并行处理。
- 多候选生成后再聚合。

Send 是 LangGraph 内部节点调度指令，不是 RabbitMQ 消息。

## 13. 失败恢复分层

### 13.1 Model Gateway 层

保留现有 Model Gateway，负责：

- Provider 适配。
- 模型能力和上下文窗口。
- 429、5xx、Timeout、Network 分类。
- 指数退避与 jitter。
- 共享 Deadline。
- 流中断恢复。
- 输出截断与续写。
- 受控云模型 fallback。
- HIGH 风险云外发禁止。
- 模型调用 Trace。

### 13.2 LangGraph Node 层

负责：

- 节点超时。
- 节点级有限重试。
- 错误写入 State。
- 路由到 compact_context 或 fallback 节点。
- Checkpoint 恢复。

### 13.3 领域 fallback 层

最终不再依赖模型：

| 失败点 | 领域降级 |
|---|---|
| Memory | 空历史 |
| Intent | 关键词规则 |
| Safety | detect_risk_signal + heuristic |
| Context / RAG | 空检索结果 |
| Response | 安全回复模板 |
| Output Safety | fail-closed 模板 |

### 13.4 避免重试乘法

必须限制 Gateway 与 LangGraph 的总调用预算：

~~~text
Gateway：
模型调用内部最多 1～2 次恢复，受共享 Deadline 限制

LangGraph：
节点最多额外执行 1 次

仍失败：
进入确定性 fallback
~~~

禁止出现 Gateway 三次乘以 Graph 三次的九次调用。

## 14. 上下文超限

PROMPT_TOO_LONG 不直接切换云模型。

流程：

~~~text
generate_response
→ Gateway 抛出 PROMPT_TOO_LONG
→ 记录错误
→ reactive_compaction_used == false
→ compact_context
→ reactive_compaction_used = true
→ 重新 generate_response
~~~

第二次仍然超限：

~~~text
→ CONTEXT_UNRECOVERABLE
→ safe_fallback
~~~

保留现有 L0、L1、L2、L3 和 Reactive 压缩规则。LangGraph 只负责显式表达状态转移。

## 15. 安全不变量

迁移后必须始终成立：

1. HIGH 风险请求零云外发。
2. 未脱敏请求零云外发。
3. 明确高危规则优先于模型结果。
4. 所有最终回复都具有匹配当前候选 ID 的 output_safety。
5. Output Safety 失败时必须 fail-closed。
6. 回复最多修改一次。
7. 高风险请求只能发送完整审核后的消息，不释放未审核 token。
8. 高风险报告与 Outbox 事件在同一 MySQL 事务提交。
9. Checkpoint 和 Trace 不保存未授权敏感正文。
10. Graph 恢复不得重复执行不可逆业务副作用。

## 16. AgentHarness 改造

AgentHarness 继续作为单轮应用服务和业务事务边界。

当前调用：

~~~python
agent_run = await create_agent_runtime(
    self.db,
    self.settings,
).run(...)
~~~

改造后：

~~~python
initial_state = {
    "turn_id": turn_id,
    "user_id": user.id,
    "session_id": session.public_id,
    "model_input": model_input,
    "artifacts": [],
    "domain_events": [],
    "errors": [],
    "revision_count": 0,
    "reactive_compaction_used": False,
    "status": "RUNNING",
}

final_state = await runtime.ainvoke(
    initial_state,
    config={
        "configurable": {
            "thread_id": turn_id,
        }
    },
)

agent_run = state_to_agent_run_result(final_state)
~~~

Graph 返回后，Harness 继续执行：

~~~text
保存用户消息
→ 必要时创建心理报告
→ 保存领域 Trace
→ 写 report.excel Outbox
→ MEDIUM/HIGH 写 case.create Outbox
→ 同一事务 commit
→ Redis best-effort 更新
→ 返回 AgentHarnessOutcome
~~~

Graph 不直接处理业务事务。

## 17. MySQL、Outbox 与 RabbitMQ

保留当前 Transactional Outbox：

~~~python
with db.begin():
    save_message()
    create_report()
    save_trace()
    add_outbox_event()
~~~

事务提交后：

~~~text
OutboxPublisher
→ RabbitMQ
→ Celery Worker
~~~

保留事件类型：

- report.excel
- case.create
- case.created
- memory.extract
- memory.summary.refresh
- memory.consolidate
- knowledge.ingest

LangGraph 不替代 RabbitMQ，也不消费这些业务事件。

Graph 节点必须避免不可逆副作用。邮件、Excel、个案和知识摄取仍由 Outbox/Celery 处理。

## 18. Checkpoint 持久化

LangGraph 只有配置 Checkpointer 后才具备状态持久化。

### 18.1 标识设计

~~~text
session_id：业务多轮会话
turn_id：单次用户请求
thread_id：单次 Graph 执行
~~~

推荐：

~~~text
thread_id = turn_id
~~~

禁止直接用 session_id 作为单轮 Graph thread_id，避免同一会话并发请求相互覆盖。

### 18.2 环境策略

| 环境 | Checkpointer |
|---|---|
| 单元测试 | InMemorySaver |
| 本地开发 | SQLite Saver 或 InMemorySaver |
| 生产 | 经过验证的持久化 Saver |

生产选项：

1. 使用成熟的 PostgreSQL Checkpointer，MySQL 继续作为业务库。
2. 使用经过验证的 MySQL Checkpointer。
3. 如果没有可靠 MySQL Saver，不得临时自研后直接宣称生产可用。

生产 Checkpointer 是上线阻塞项。使用 InMemorySaver 时只能宣称完成 LangGraph 编排迁移，不能宣称支持进程重启恢复。

### 18.3 Checkpoint 与业务数据边界

Checkpoint 保存：

- 脱敏 AgentState。
- 已完成节点。
- 下一步节点。
- 并行 pending writes。
- Artifact 和领域 Event。
- 错误与恢复状态。

MySQL 业务表保存：

- 聊天消息。
- 心理报告。
- 长期记忆。
- 领域 Trace。
- Outbox。

Checkpoint 不是业务事实数据库。

## 19. SSE 流式输出

ChatService 负责消费 LangGraph 事件流：

~~~python
async for event in runtime.astream(...):
    if event.kind == "token":
        yield sse("token", ...)
    elif event.kind == "replace":
        yield sse("replace", ...)
    elif event.kind == "final":
        yield sse("done", ...)
~~~

投递策略：

| 风险 | 行为 |
|---|---|
| LOW / MEDIUM | 可发送经过分段检查的流，最终不一致时发送 replace |
| HIGH | 完整缓冲，审核后一次性发送 message |

LangGraph 提供事件流，但内容释放策略仍由 MindBridge 决定。

客户端断线恢复需要额外实现：

- turn_id / request_id。
- GENERATING / COMPLETED / FAILED 状态。
- 后台 Graph 执行与连接生命周期解耦。
- SSE 事件序号。
- Last-Event-ID。
- 事件缓冲或持久化。
- 重连接口。

Checkpoint 解决 Graph 执行恢复，不自动解决 SSE 消息重放。

## 20. 可观测性

### 20.1 LangGraph Checkpoint

用于：

- 执行状态恢复。
- 节点进度检查。
- 中间 State 分析。

不作为业务审计日志。

### 20.2 LangSmith

用于：

- Graph 路径。
- 节点输入输出。
- 节点耗时。
- LLM 调用树。
- Token、模型和 Provider 延迟。
- 重试和失败。
- Prompt / 模型评测。

开发和测试环境可以使用虚构数据开启完整 Trace。

生产默认：

- 禁止上传原始心理正文。
- 只允许字段白名单 Metadata。
- 或关闭 LangSmith 正文 Trace。

### 20.3 本地 MySQL Trace

继续保存领域审计：

- turn_id。
- intent / risk。
- Artifact ID。
- 知识引用。
- SAFETY_OVERRIDE。
- REVISION_REQUESTED。
- FINAL_ACCEPTED。
- FALLBACK_APPLIED。
- 报告 ID。
- Outbox 关联 ID。

删除 AgentStep 与 agent_event 的重复序列化。

### 20.4 Prometheus

继续记录：

- 节点成功率。
- 节点重试次数。
- fallback 比例。
- Safety Override 比例。
- p50 / p95 / p99 延迟。
- Provider 成功率。
- Context 压缩层级。
- Memory / RAG 命中率。

### 20.5 全链路关联

~~~text
MindBridge turn_id
= LangGraph thread_id
= LangSmith metadata.turn_id
= model_call_trace.turn_id
= agent_run_trace.turn_id
~~~

## 21. 隐私要求

禁止向 Checkpoint 或外部 Trace 写入：

- 原始用户输入。
- 心理报告正文。
- 完整长期记忆正文。
- 完整 Prompt。
- 未审核模型输出。
- 手机号、身份证、学号等 PII。

允许保存：

- 脱敏 model_input。
- Artifact ID。
- 风险等级。
- 节点状态。
- 有界受控摘要。
- Token、延迟、错误码。
- 不含正文的检索引用。

Checkpoint 存储必须具备：

- 访问控制。
- 静态加密。
- 保留和删除策略。
- 用户删除后的关联清理。
- 禁止在异常日志中打印完整 State。

## 22. 代码目录规划

新增：

~~~text
app/graph/
├─ __init__.py
├─ state.py
├─ artifacts.py
├─ errors.py
├─ nodes.py
├─ routing.py
├─ workflow.py
├─ runtime.py
├─ checkpoint.py
└─ tracing.py
~~~

文件职责：

| 文件 | 职责 |
|---|---|
| state.py | AgentState、Reducer、状态常量 |
| artifacts.py | Artifact Schema 和构造函数 |
| errors.py | 节点错误与恢复分类 |
| nodes.py | 各 Graph 节点 |
| routing.py | 条件边与验收路由 |
| workflow.py | StateGraph 构建和 compile |
| runtime.py | Graph 与 AgentRunResult 适配 |
| checkpoint.py | 不同环境 Checkpointer 工厂 |
| tracing.py | LangGraph、LangSmith、本地 Trace 桥接 |

保留并修改：

~~~text
app/agents/harness.py
app/agents/result.py
app/agents/factory.py
app/agents/autonomous.py
app/services/trace.py
app/services/model_trace.py
app/services/chat.py
app/core/config.py
~~~

逐步废弃：

~~~text
app/agents/coordinator.py
app/agents/registry.py
app/agents/event_driven_runtime.py
app/agents/recovery.py 中的通用重试部分
app/agents/events.py 中的 Task / Claim / 纯调度 Event
~~~

旧文件不能在第一提交直接删除，必须先完成 Feature Flag 和行为对比。

## 23. 配置项

建议增加：

~~~text
AGENT_RUNTIME=custom|langgraph

LANGGRAPH_CHECKPOINTER=memory|sqlite|postgres|mysql
LANGGRAPH_CHECKPOINT_TTL_SECONDS=
LANGGRAPH_NODE_TIMEOUT_SECONDS=
LANGGRAPH_NODE_MAX_ATTEMPTS=

LANGSMITH_TRACING_ENABLED=false
LANGSMITH_PROJECT=
LANGSMITH_ALLOW_CONTENT=false

SSE_RESUME_ENABLED=false
~~~

所有生产默认值必须 fail-closed：

- LangSmith 正文 Trace 默认关闭。
- HIGH 云外发始终关闭。
- 没有生产 Checkpointer 时不宣称可恢复。

## 24. 分阶段实施

### 阶段 0：冻结契约

冻结并补测试：

- AgentRunResult。
- Artifact Schema。
- RiskLevel。
- OutputSafetyStatus。
- Harness 输入输出。
- Outbox 事件。
- Trace 必要字段。

### 阶段 1：引入依赖和 State

任务：

1. 增加 LangGraph 依赖。
2. 新建 app/graph/state.py。
3. 新建 Artifact Schema。
4. 增加 Agent Runtime Feature Flag。
5. 不改变默认 Runtime。

### 阶段 2：包装现有 Agent 逻辑

任务：

1. 实现 prefetch_memory。
2. 实现 understand_intent。
3. 实现 assess_safety。
4. 实现 gather_context。
5. 实现 generate_response。
6. 实现 review_response。
7. 实现 safe_fallback。
8. 实现 finalize。

不得在此阶段重写 Model Gateway、Memory 或 RAG。

### 阶段 3：构建 Graph

任务：

1. Memory 前置。
2. Understanding 与 Safety 并行。
3. Context 条件路由。
4. Response 生成。
5. Output Safety。
6. 一次修改循环。
7. Finalize 验收。
8. Artifact 与领域 Event 合并。

### 阶段 4：错误恢复

任务：

1. 配置节点 Deadline。
2. 配置有限 RetryPolicy。
3. 实现 ModelError 路由。
4. 实现 Reactive Compact。
5. 实现各节点确定性 fallback。
6. 校验 Gateway 与 Graph 总重试预算。

### 阶段 5：Harness 适配

任务：

1. LangGraphAgentRuntime 输出 AgentRunResult。
2. Harness 根据 Feature Flag 选择 Runtime。
3. 保持 MySQL 事务和 Outbox 不变。
4. 保持 Redis 更新语义不变。
5. 保持 API DTO 不变。

### 阶段 6：Checkpoint

任务：

1. 单测使用 InMemorySaver。
2. 验证 State 可序列化。
3. 接入生产持久化 Saver。
4. 验证进程重启恢复。
5. 验证并行成功节点不重复执行。
6. 完成隐私和保留策略。

### 阶段 7：流式输出

任务：

1. 将 Graph token / update 适配到现有 SSE。
2. 保持 HIGH 完整缓冲。
3. 保持 replace 语义。
4. 保证非法候选回复不能发出 done。
5. 可选实现断线恢复。

### 阶段 8：可观测性

任务：

1. 接入 LangGraph Node Trace。
2. 开发环境接入 LangSmith。
3. 生产应用 Trace 字段白名单。
4. 本地 AgentTraceService 改为领域事件记录。
5. 增加 Prometheus 节点指标。
6. 统一 turn_id。

### 阶段 9：灰度与删除旧 Runtime

任务：

1. 默认继续使用 custom。
2. 在离线评测和测试环境启用 langgraph。
3. 小比例灰度。
4. 对比安全不变量和资源消耗。
5. 完成回滚演练。
6. 稳定后切换默认值。
7. 最后删除旧 Coordinator、Registry 和 Task 协议。

## 25. 测试要求

### 25.1 节点单测

每个节点覆盖：

- 正常输出。
- 输入缺失。
- Provider Timeout。
- Provider Permanent Error。
- fallback Artifact。
- Artifact JSON 序列化。
- 隐私字段过滤。

### 25.2 Graph 路径测试

必须覆盖：

1. CHAT + LOW 跳过完整 Context。
2. CONSULT + LOW 加载 Context。
3. MEDIUM 加载 Context。
4. HIGH 加载 Context并禁止云模型。
5. Understanding 与 Safety 并行。
6. Response 后强制 review。
7. Review 与候选 ID 不匹配时拒绝 finalize。
8. REVISE 后重新生成。
9. 第二次仍失败后 fallback。
10. Prompt超限只 Reactive Compact 一次。
11. Safety失败后规则兜底。
12. Context失败后空检索继续。
13. Redis失败后回退 MySQL。
14. Checkpoint恢复不重复成功节点。

### 25.3 业务集成测试

必须覆盖：

- Harness事务回滚。
- 心理报告与Outbox同事务。
- MEDIUM/HIGH创建case.create。
- HIGH个案后创建case.created。
- RabbitMQ事件不由Graph直接发送。
- 消费者幂等。
- SSE token/message/replace/done顺序。

### 25.4 离线对比

使用同一数据运行旧 Runtime 与 LangGraph Runtime，对比：

- Intent。
- Risk。
- 高风险召回率。
- 最终安全状态。
- RAG命中。
- fallback比例。
- 模型调用次数。
- 云外发次数。
- p95延迟。
- Trace完整性。

不要求回复文本逐字一致，但安全不变量必须完全一致。

## 26. 验收标准

上线前必须满足：

1. 现有 Python测试全部通过。
2. 新增 LangGraph节点与路径测试全部通过。
3. HIGH风险云外发次数为0。
4. 未脱敏云外发次数为0。
5. 所有最终回复存在匹配的output_safety。
6. 安全审核异常全部fail-closed。
7. Prompt超限最多Reactive一次。
8. 报告与Outbox保持同事务。
9. Redis故障不阻断对话。
10. Graph State中不存在原始用户输入。
11. 生产Checkpoint可以进程重启恢复。
12. Trace不存在未授权心理正文。
13. Feature Flag可以无数据迁移地回滚到旧Runtime。

## 27. 回滚策略

必须保留：

~~~text
AGENT_RUNTIME=custom
~~~

回滚只切换新请求的 Runtime。已开始的 LangGraph turn 根据 thread_id继续完成或标记失败，不强制转换为旧 Blackboard。

业务表、Artifact和 AgentRunResult契约保持兼容，确保切换 Runtime不需要迁移聊天消息、报告或Outbox数据。

## 28. 实施时禁止事项

1. 禁止为了使用 LangGraph 重写全部领域服务。
2. 禁止同时删除旧 Runtime 和引入新 Runtime。
3. 禁止让 Graph 节点直接发送邮件、写 Excel 或发布 RabbitMQ。
4. 禁止将原始心理数据发送到 LangSmith。
5. 禁止把 Checkpoint 当业务数据库。
6. 禁止 Gateway 与 Graph无界叠加重试。
7. 禁止在没有持久化 Saver时宣称支持故障恢复。
8. 禁止只检查 output_safety存在而不校验responseArtifactId。
9. 禁止 HIGH 风险提前释放未审核 Token。
10. 禁止为保留旧设计而在固定任务图中滥用 Router、Send和能力竞标。

## 29. 改造完成后的简历表述

建议调整为：

> Agent Harness 与 LangGraph 多 Agent Runtime：设计单轮对话级 Agent Harness，统一处理输入脱敏、会话持久化、Trace 与 Transactional Outbox；基于 LangGraph StateGraph 构建共享 Typed State，以结构化 Artifact 承载意图、风险、上下文、候选回复与安全审核结果，实现意图/风险并行分析、按需 RAG、回复安全门控、节点级重试、确定性降级及持久化 Checkpoint 恢复；保留 Model Gateway 的隐私约束与受控云路由，确保 HIGH 风险请求零云外发。

不再宣称自研：

- Coordinator任务认领框架。
- Blackboard调度器。
- 通用节点重试。
- Checkpoint恢复基础设施。

仍然可以强调自研：

- Agent Harness业务边界。
- Artifact领域契约。
- 高风险安全门控。
- Model Gateway隐私策略。
- Context Planner。
- Memory Dream。
- PDF RAG。
- Transactional Outbox。
- 本地隐私Trace。

## 30. 最终原则

~~~text
LangGraph负责：
共享State
Graph调度
并行节点
条件路由
节点重试
Checkpoint
执行事件流

MindBridge负责：
Harness
Artifact契约
风险规则
安全验收
Model Gateway
上下文治理
Memory / RAG
隐私Trace
MySQL业务事务
Transactional Outbox
RabbitMQ / Celery
~~~

核心决策：

> 使用 LangGraph替换通用调度基础设施，不把领域安全策略交给框架；保留共享 Blackboard的设计思想，但使用可Checkpoint的 Typed State重新实现；Graph负责可恢复计算，Harness继续负责业务事务和异步副作用。
