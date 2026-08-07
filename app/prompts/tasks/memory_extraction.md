任务：从本轮对话提取值得跨会话保存的长期记忆候选，并与 existingMemoryIndex 仲裁。

规则：
- 候选必须具体、可撤销，且 evidenceMessageIds 只能引用 recentDialogue 中 USER 消息的真实 ID。
- memoryKey 表示稳定语义槽位；同一事实即使改名也必须使用相同 memoryKey。
- action 只能是 CREATE、CONFIRM、SUPERSEDE、CONFLICT、IGNORE。
- CONFIRM、SUPERSEDE、CONFLICT 的 relatedMemoryIds 只能引用 existingMemoryIndex 中的真实 ID；无法确定时使用 CONFLICT，不得编造 ID。
- 临时情绪、模型推测、诊断、高风险判断和敏感身份信息不得写入长期记忆。
- reason 简述动作依据，不得加入对话中没有的事实。

只输出 memory_candidate_v3 JSON 数组，不输出 Markdown 或解释。
