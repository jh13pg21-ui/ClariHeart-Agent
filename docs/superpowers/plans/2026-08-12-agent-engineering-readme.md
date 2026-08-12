# Agent Engineering README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ClariHeart Agent 根 README 优化为面向 Agent 开发工程师岗位的开源项目展示页，并把最新分支合并到公开仓库 `main`。

**Architecture:** 只调整 `README.md` 的信息层级和文案，首屏提供定位、技术标签、导航、可核验的 Agent 工程能力与测试证据；现有详细技术章节继续作为深读内容。新增链接全部使用仓库内相对路径，业务代码和技术标识保持不变。

**Tech Stack:** Markdown、Git、GitHub、pytest、Node.js test runner

## Global Constraints

- 展示名称固定为 `ClariHeart Agent`。
- 技术标识 `MindBridgeAgentHarness`、模型名、数据库名和环境变量前缀不改名。
- 不虚构用户量、准确率、性能、线上规模或临床效果。
- 不提交现有两个未跟踪的 2026-08-05 设计文档。

---

### Task 1: 重构 README 首屏

**Files:**
- Modify: `README.md:1-48`

**Interfaces:**
- Consumes: `app/agents/`、`app/context/`、`app/llm/`、`app/services/long_term_memory.py`、`app/rag_ingestion/`、`tests/`
- Produces: 首屏定位、技术标签、导航和“30 秒项目速览”代码入口

- [ ] **Step 1: 记录当前首屏结构**

Run: `Get-Content -Encoding utf8 README.md -TotalCount 60`

Expected: 标题后直接进入“核心能力”，尚无技术标签、短导航或代码证据表。

- [ ] **Step 2: 写入首屏内容**

将标题后的介绍重构为以下顺序：

```markdown
# ClariHeart Agent

> 面向校园心理支持场景的生产级 AI Agent 系统：围绕 Multi-Agent 协作、上下文工程、长期记忆、RAG、模型可靠性与高风险安全边界构建。

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Multi--Agent](https://img.shields.io/badge/Architecture-Multi--Agent-6C63FF)
![RAG](https://img.shields.io/badge/RAG-Hybrid%20Retrieval-0A7BBB)
![Ollama](https://img.shields.io/badge/LLM-Ollama-000000?logo=ollama&logoColor=white)
![Celery](https://img.shields.io/badge/Async-Celery-37814A?logo=celery&logoColor=white)

[工程亮点](#30-秒项目速览) · [系统架构](#系统架构) · [快速启动](#快速启动docker-compose) · [测试验收](#测试与工程验收) · [项目边界](#当前边界)

## 30 秒项目速览

| Agent 工程维度 | 项目实现 | 代码证据 |
|---|---|---|
| Multi-Agent 编排 | Coordinator、Understanding、Safety、Context、Response 基于任务板、共享黑板与 artifact 协作，安全审查独立于回复生成 | [`app/agents/`](app/agents/) |
| Prompt / Context Engineering | 显式版本化 Prompt Registry、可信/不可信内容分区、token 预算、L0–L3 压缩和一次性 reactive recovery | [`app/prompts/`](app/prompts/) · [`app/context/`](app/context/) |
| 长期记忆 | MySQL 权威记录 + Redis 短期缓存；记忆保存证据、版本与冲突状态，并通过 Dream 门控异步整理 | [`long_term_memory.py`](app/services/long_term_memory.py) · [`memory_consolidation.py`](app/services/memory_consolidation.py) |
| 模型可靠性 | 统一 Gateway 提供类型化错误、deadline、有界重试、流恢复、输出续写和受控云降级 | [`app/llm/`](app/llm/) |
| RAG 摄取 | LiteParse、PaddleOCR 与 Vision 双路由，Canonical Document JSON、父子块和版本化原子激活 | [`app/rag_ingestion/`](app/rag_ingestion/) |
| 安全与治理 | 高风险请求禁止云外发；认证、CSRF、幂等工具、Transactional Outbox、限流与死信共同约束副作用 | [`output_safety.py`](app/services/output_safety.py) · [`app/workers/`](app/workers/) |
| 工程验证 | 完整 Python 测试 351 项通过、前端测试 9 项通过，并提供离线故障 Harness | [`tests/`](tests/) · [`app/harness/`](app/harness/) |
```

- [ ] **Step 3: 检查新增链接目标**

Run: `Test-Path app/agents; Test-Path app/context; Test-Path app/llm; Test-Path app/services/long_term_memory.py; Test-Path app/rag_ingestion; Test-Path tests`

Expected: 六项全部输出 `True`。

### Task 2: 优化深读入口

**Files:**
- Modify: `README.md:556-565`

**Interfaces:**
- Consumes: 现有“面试讲述建议”四个工程问题
- Produces: 更符合开源项目语气的“关键工程问题”章节

- [ ] **Step 1: 重命名章节并调整引导语**

将 `## 面试讲述建议` 改为 `## 关键工程问题`，将“可以围绕”改为“项目重点解决”，保留四个已有且可由代码证明的问题。

- [ ] **Step 2: 核对品牌与技术标识**

Run: `rg -n "ClariHeart Agent|MindBridge" README.md`

Expected: 展示名称仅使用 `ClariHeart Agent`；`MindBridge` 只出现在需要保持兼容的技术类名中。

### Task 3: 验证并提交 README

**Files:**
- Modify: `README.md`
- Verify: `docs/superpowers/specs/2026-08-12-agent-engineering-readme-design.md`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的 Markdown 结果
- Produces: 可合并的文档提交

- [ ] **Step 1: 检查 Markdown 与冲突标记**

Run: `git diff --check; rg -n "^(<<<<<<<|=======|>>>>>>>)" README.md`

Expected: `git diff --check` 成功；`rg` 无匹配。

- [ ] **Step 2: 运行完整 Python 测试**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: `351 passed, 7 skipped`，退出码为 0。

- [ ] **Step 3: 运行前端测试**

Run: `node --test tests/frontend/*.test.cjs`

Expected: `9 passed`，退出码为 0。

- [ ] **Step 4: 只提交计划内文件**

```bash
git add README.md docs/superpowers/plans/2026-08-12-agent-engineering-readme.md
git commit -m "docs: optimize README for Agent engineering roles"
```

### Task 4: 推送、合并并核验公开仓库

**Files:**
- Verify: Git branch refs and public GitHub repository

**Interfaces:**
- Consumes: 已通过测试的 `0808_production-context-memory-recovery`
- Produces: 包含最新 79 个功能提交和求职展示 README 的远端 `main`

- [ ] **Step 1: 推送最新分支**

Run: `git push origin 0808_production-context-memory-recovery`

Expected: 远端分支更新到本地 HEAD。

- [ ] **Step 2: 合并到 main**

在 GitHub 创建从 `0808_production-context-memory-recovery` 到 `main` 的 Pull Request，确认可合并后执行 merge。

- [ ] **Step 3: 同步并核验本地引用**

Run: `git fetch origin; git rev-list --left-right --count origin/main...origin/0808_production-context-memory-recovery`

Expected: 输出 `0 0`，两个远端分支指向相同内容或 feature 已完整包含于 `main`。

- [ ] **Step 4: 核验公开页面**

访问 `https://github.com/jh13pg21-ui/ClariHeart-Agent`，确认仓库为 `Public`、默认分支为 `main`、首屏标题为 `ClariHeart Agent`，并能看到“30 秒项目速览”。
