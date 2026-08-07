任务：对 memoryIndex 中的长期记忆执行低频 Dream 整理。

规则：
- 只允许 KEEP、MERGE、SUPERSEDE、EXPIRE；sourceMemoryIds 只能引用输入中的真实 ID，且每个 ID 最多出现一次。
- MERGE 仅用于语义相同且不冲突的记忆，至少包含两个来源，并生成 mergedMemory；合并后的 body 必须完整保留稳定事实，不能只选择第一条。
- SUPERSEDE 的第一个 ID 是保留项，其余项是被替代的旧版本。
- EXPIRE 仅用于已经失效且有明确时间依据的内容；不确定时 KEEP。
- 冲突内容不得静默合并；敏感推测、诊断和风险判断不得升级为事实。
- mergedMemory.memoryKey 必须是稳定的 ASCII 语义槽位，reason 说明合并依据。

只输出 memory_consolidation_v2 JSON 对象，不输出 Markdown 或解释。
