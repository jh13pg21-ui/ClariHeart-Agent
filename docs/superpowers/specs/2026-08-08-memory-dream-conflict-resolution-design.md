# MindBridge Memory Dream V3 设计

## 目标

将现有长期记忆 V2 从“同名覆盖式版本化 + 默认关闭的浅层整理”升级为可上线的记忆冲突仲裁与 Dream 维护闭环：写入时不静默吞掉矛盾，低频任务能够真正合并正文、证据与血缘，并使用与真实 Claude Code 相同语义的时间、扫描、会话、租约四层门控。

## 非目标

- 不把长期记忆改成向量数据库；现有模型选择与确定性排序继续使用。
- 不物理删除历史记忆；淘汰继续通过状态表达。
- 不在高风险对话中提取长期记忆。
- 不让 Dream 阻塞学生端响应。

## 数据模型

### LongTermMemory

新增以下字段：

- `memory_key`：同一用户下稳定的事实槽位，例如 `preference.response_detail`；模型提取必须复用已有槽位，确定性降级使用类型与名称规范化生成。
- `conflict_group_id`：冲突候选所属组；`CONFLICTED` 记忆不参与正常检索和 Prompt 注入。
- `consolidated_from_ids_json`：新合并版本的来源 public ID 列表。
- `resolution_reason`：脱敏后的版本替换、冲突或整理理由。

状态扩展为 `ACTIVE / SUPERSEDED / EXPIRED / CONFLICTED`。旧数据迁移时根据 `memory_type + name` 生成稳定 key。

### MemoryDreamState

每个用户一行，保存：

- `last_scanned_at`：扫描节流水位；
- `last_consolidated_at`：成功 Dream 水位；
- `lease_owner`、`lease_acquired_at`、`lease_expires_at`：数据库租约；
- `last_error`：脱敏错误；
- 创建与更新时间。

`user_id` 唯一。非 SQLite 数据库通过 `SELECT ... FOR UPDATE` 串行化调度；租约超时后可以回收，并把关联的 `QUEUED/RUNNING` run 标记为 `ABANDONED`。

## 写入时冲突仲裁

记忆候选协议升级为 V3，新增：

- `memoryKey`；
- `action`：`CREATE / CONFIRM / SUPERSEDE / CONFLICT / IGNORE`；
- `relatedMemoryIds`；
- `reason`。

所有 related ID 必须来自传给模型的当前用户记忆索引，否则候选拒绝。

确定性决策顺序：

1. 内容 hash 完全相同：追加证据、确认次数和最大置信度；如果命中旧的 `SUPERSEDED` 版本且当前槽位已有其他 ACTIVE 版本，则重新激活该事实并关闭当前版本，表达用户回退偏好。
2. `IGNORE`：不写入。
3. `CONFLICT`：创建 `CONFLICTED` 候选，保留现有 ACTIVE 事实；冲突组不会注入 Prompt。
4. `SUPERSEDE`：只允许替换白名单中的 ACTIVE/CONFLICTED 记录，创建新 ACTIVE 版本并保留旧行。
5. 相同 `memory_key` 的新事实：按最新的、具有用户消息证据的明确表达创建新版本；旧 ACTIVE 行转为 `SUPERSEDED`。
6. 其余候选创建 ACTIVE 记录。

旧的 V2/确定性候选继续兼容，由服务生成稳定 memory key 并按 UPSERT 语义处理。

## Dream 四层触发

`reserve_schedule()` 在同一数据库事务内执行：

1. **时间门控**：距上次成功 Dream 至少 24 小时；首次运行不受限制。
2. **扫描节流**：距上次资格扫描至少 60 分钟；周期扫描和提取后扫描共用同一水位。
3. **会话门控**：自上次成功 Dream 以来至少 5 个用户会话发生修改。
4. **租约门控**：没有未过期租约；默认租约 1 小时，过期可回收。

此外保留内容规模条件：至少 10 条 `ACTIVE/CONFLICTED` 记忆值得执行重型整理。这个条件是工作量阈值，不替代四层门控。

触发来源有两个：

- 每次长期记忆提取完成后进行低成本资格检查；
- Celery Beat 每 15 分钟执行全局扫描，分批检查启用长期记忆的用户。

满足条件后创建 `MemoryConsolidationRun(QUEUED)`、获取租约，并通过 Transactional Outbox 发送 `memory.consolidate`。同一用户同一时刻最多有一个有效 run。

## Dream 整理协议

模型只能返回以下动作：

- `KEEP`：保留来源；
- `MERGE`：必须提供新的结构化 `mergedMemory`；服务端合并全部来源证据，创建新版本，再将来源标记为 `SUPERSEDED`；
- `SUPERSEDE`：必须提供 `survivorMemoryId`，其他来源转为 `SUPERSEDED`；
- `EXPIRE`：来源转为 `EXPIRED`；
- `CONFLICT`：来源转为同一 `CONFLICTED` 组，不再注入 Prompt。

所有 ID、动作、字段和长度均做严格白名单校验。一个来源 ID在一轮输出中只能被消费一次。非法输出零业务变更，只更新 run 错误状态。

当 `MERGE` 正文与某个来源完全一致时复用该来源作为 survivor，并合并证据；否则创建新的 ACTIVE 行，写入 `consolidated_from_ids_json`。合并不相信模型提供的证据 ID，证据只从数据库来源记录求并集。

## 失败恢复与幂等

- Outbox 和 `ProcessedMessage` 保证消息幂等；
- 模型/网络临时失败由 Celery 有界重试；租约在重试期间保持；
- Worker 丢失后租约到期，下一次 Beat 扫描回收并重新调度；
- 非法模型输出不重试，不修改长期记忆，释放租约并记录 `INVALID_OUTPUT`；
- 成功、永久失败和非法输出都会结束 run；成功更新 `last_consolidated_at`；
- 所有历史行软保留，可根据 consolidation run 的 source/result JSON 审计。

## 检索安全

正常列表、相关记忆选择和 Prompt 注入只读取未过期的 `ACTIVE` 记录。`CONFLICTED`、`SUPERSEDED`、`EXPIRED` 只供 Dream、审计和用户管理使用，避免互相矛盾的事实同时影响回答。

## 配置默认值

- `MEMORY_CONSOLIDATION_ENABLED=true`
- `MEMORY_CONSOLIDATION_MIN_INTERVAL_HOURS=24`
- `MEMORY_CONSOLIDATION_SCAN_INTERVAL_MINUTES=60`
- `MEMORY_CONSOLIDATION_MIN_ACTIVE_MEMORIES=10`
- `MEMORY_CONSOLIDATION_MIN_MODIFIED_SESSIONS=5`
- `MEMORY_CONSOLIDATION_LEASE_SECONDS=3600`
- `MEMORY_CONSOLIDATION_SCAN_BATCH_SIZE=100`
- `MEMORY_CONSOLIDATION_BEAT_INTERVAL_MINUTES=15`

## 测试要求

- 迁移在 SQLite 与 MySQL 兼容路径上通过；
- exact confirm、同槽位 supersede、显式 conflict、revert/reactivate 分别有测试；
- 四道门控逐一关闭时不调度；租约过期可以恢复；
- `MERGE` 创建真实合并版本并合并证据；
- 非法/越界 ID 零修改；
- Beat 扫描可在没有新对话时调度；
- 重复事件只有一次副作用；
- 冲突记忆不能进入相关记忆结果；
- 全量测试与工程 harness 通过。
