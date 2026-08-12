# Async Agent and Actual Output Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将多 Agent Runtime 改为阶段内真实并行，并让 SafetyAgent 审核最终候选文本。

**Architecture:** Stage 1 并行执行意图、风险和记忆预取，Coordinator 确定性合并；随后构建上下文、由 ResponseAgent 生成候选文本、SafetyAgent 审核真实文本。同步数据库写入留在 Harness 的明确事务边界。

**Tech Stack:** asyncio.TaskGroup、httpx.AsyncClient、SQLAlchemy、unittest。

## Global Constraints

- HIGH Safety Override 永远不能被低风险结果覆盖。
- 并行任务不共享同步 SQLAlchemy Session。
- HIGH 不输出未审核 Token。
- 最多一次模型修订，失败使用安全模板。

---

### Task 1: 异步化 AiClient 和 Agent 模型调用

**Files:**
- Modify: `app/services/ai.py`
- Modify: `app/services/agent_models.py`
- Create: `tests/test_async_ai.py`

**Interfaces:**
- Produces: `await AiClient.complete(messages) -> str`
- Produces: `AiClient.stream(messages) -> AsyncIterator[str]`

- [ ] **Step 1: 写异步失败测试**

```python
async def test_complete_uses_async_transport(self):
    result = await AiClient(settings).complete([AiMessage(role="user", content="hello")])
    self.assertEqual(result, "mock response")
```

- [ ] **Step 2: 验证现有同步接口失败**

Run: `python -m unittest tests.test_async_ai -v`  
Expected: FAIL，返回值不可 await。

- [ ] **Step 3: 使用共享 AsyncClient**

Ollama/OpenAI completion 和 stream 统一使用 `httpx.AsyncClient`，配置连接、读取和总超时；保留 Mock Provider 的异步行为。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_async_ai -v`  
Expected: PASS

Commit: `refactor: make model clients fully asynchronous`

### Task 2: Stage 1 并行与确定性合并

**Files:**
- Modify: `app/agents/registry.py`
- Modify: `app/agents/autonomous.py`
- Modify: `app/agents/coordinator.py`
- Modify: `app/agents/event_driven_runtime.py`
- Modify: `app/agents/harness.py`
- Modify: `app/services/chat.py`
- Create: `tests/test_async_agent_runtime.py`

**Interfaces:**
- Produces: `await EventDrivenCoordinator.run(board) -> CollaborationBlackboard`
- Produces: `await EventDrivenAgentRuntimeService.run(...) -> AgentRunResult`
- Produces: `await MindBridgeAgentHarness.run(...) -> AgentHarnessOutcome`

- [ ] **Step 1: 写并行时间测试**

创建三个各等待 100ms 的 DemoAgent，断言 Stage 1 总耗时小于 220ms，而不是约 300ms。

```python
started = time.perf_counter()
result = await coordinator.run(board)
self.assertLess(time.perf_counter() - started, 0.22)
```

- [ ] **Step 2: 写 HIGH 合并优先级测试**

同时产生 LOW 与 HIGH 风险 Artifact，断言最终风险为 HIGH 且存在 `SAFETY_OVERRIDE`。

- [ ] **Step 3: 实现 TaskGroup 批处理**

所有候选基于同一 `round_board` 执行：

```python
async with asyncio.TaskGroup() as group:
    pending = [(item, group.create_task(item.agent.act(item.task, round_board))) for item in candidates]
```

任务完成后按安全事件、风险、意图、记忆、其他 Artifact 的固定顺序应用结果。

- [ ] **Step 4: 拆分 Context 预取与构建**

预取阶段只读取 Redis/MySQL 已预加载历史；RAG 和 Skill 选择在意图与风险确定后运行。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest tests.test_async_agent_runtime tests.test_event_driven_multi_agent -v`  
Expected: PASS

Commit: `feat: run independent agent analysis concurrently`

### Task 3: ResponseAgent 生成真实候选文本

**Files:**
- Modify: `app/agents/events.py`
- Modify: `app/agents/result.py`
- Modify: `app/agents/autonomous.py`
- Modify: `app/agents/event_driven_runtime.py`
- Create: `tests/test_response_candidate.py`

**Interfaces:**
- Produces: `response_candidate` Artifact，payload 含 `text`、`model`、`provider`、`latencyMs`
- Consumes: Context、Intent、Risk Artifact

- [ ] **Step 1: 写失败测试**

```python
candidate = board.latest_artifact("response_candidate")
self.assertIsInstance(candidate.payload["text"], str)
self.assertTrue(candidate.payload["text"])
self.assertNotIn("messages", candidate.payload)
```

- [ ] **Step 2: 实现候选生成**

ResponseAgent 调用 `self.client().complete(messages)`，而不是仅发布 Prompt。Trace 只保存 Prompt 摘要哈希，不默认保存完整 System Prompt。

- [ ] **Step 3: 更新 Runtime 结果**

`AgentRunResult` 增加 `response_text: str` 和 `output_safety`，移除由 ChatService 再次调用模型生成文本的责任。

- [ ] **Step 4: 验证并提交**

Run: `python -m unittest tests.test_response_candidate -v`  
Expected: PASS

Commit: `feat: generate response candidates inside response agent`

### Task 4: 审核实际文本和分级发送

**Files:**
- Create: `app/services/output_safety.py`
- Modify: `app/agents/autonomous.py`
- Modify: `app/agents/coordinator.py`
- Modify: `app/services/chat.py`
- Modify: `app/agents/harness.py`
- Create: `tests/test_output_safety.py`
- Create: `tests/test_chat_delivery_policy.py`

**Interfaces:**
- Produces: `OutputSafetyDecision(status, reason, safe_text)`
- Produces: `safe_high_risk_fallback(display_name: str) -> str`

- [ ] **Step 1: 写泄露与危险细节失败测试**

```python
self.assertEqual(review("你的风险等级是 HIGH").status, "REVISE")
self.assertEqual(review("下面是具体自伤步骤……").status, "FALLBACK")
self.assertEqual(review(valid_crisis_response).status, "APPROVED")
```

- [ ] **Step 2: 实现确定性规则**

检测后台标签、评分、危险方法、空文本和高风险必要要素。HIGH 必须包含现实支持、当前安全和紧急资源方向。

- [ ] **Step 3: SafetyAgent 审核 `response_candidate.text`**

允许一次修订；第二次失败发布 Fallback Artifact。Coordinator 只接受 APPROVED 或 FALLBACK。

- [ ] **Step 4: 实现发送策略**

- CHAT/LOW：将已接受文本分块为 SSE Token。
- MEDIUM：审核后分块发送。
- HIGH：审核后发送单个 `message` SSE 事件。
- 所有路径最后发送 `done`。

- [ ] **Step 5: 验证并提交**

Run: `python -m unittest tests.test_output_safety tests.test_chat_delivery_policy -v`  
Expected: PASS

Commit: `feat: review actual model output before delivery`

### Milestone Verification

Run:

```powershell
python -m unittest tests.test_async_ai tests.test_async_agent_runtime tests.test_response_candidate tests.test_output_safety tests.test_chat_delivery_policy -v
python -m unittest discover -s tests
```

Expected: 全部 PASS，Stage 1 时间测试证明真实并发。

