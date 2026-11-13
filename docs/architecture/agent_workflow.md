# Agent 工作流

用户消息进入 `/api/chat` 后：

1. `ChatAgent` 使用原生工具调用能力选择下一步要使用的工具。
2. 工作区工具可以列出文件、读取文件以及搜索本地代码。
3. 记忆工具用于检索当前范围内的对话历史。RAG 用于检索可复用的非结构化证据，例如 Issue 描述、PR 讨论、失败的 CI 日志、项目文档、用户上传的知识以及经过确认的记忆笔记。工作区工具和结构化工具则负责处理当前代码与实时事实。
4. 专项 Agent 分别执行 Issue、PR、CI、报告或安全分析。
5. `SafetyAgent` 负责处理对外写入意图。在 MVP 阶段，它只生成操作草稿，不会直接写回 GitHub。
6. 后端记录工具调用、引用来源、最终回答以及持久化的聊天记忆。

对于涉及多个领域的工程决策，`ChatAgent` 可以调用 `workflow.run_engineering_review`：

1. `PlannerAgent` 调用大模型提出结构化计划。程序校验任务白名单、可用目标、依赖和环路，再绑定服务端 claim，生成 `WorkflowSpec`。模型不能生成任意函数或改变仓库范围。
2. `WorkflowOrchestrator` 在依赖条件满足时，并行执行已经就绪的任务。
3. 现有的 Issue、PR、CI、RAG、仓库健康检查和安全能力会作为工作流任务执行。
4. `ObserverAgent` 先执行硬规则。已有明确 finding 时不追加模型；无硬规则问题且至少有两个可比较事实时，模型仅检查事实间的明确矛盾或被另一事实反驳的结论。所有模型发现必须带两个不同事实的原文引用，程序校验事实 ID、任务 ID 和原文；这并不能形式化证明模型逻辑正确。
5. 必要时 Planner 先按规则追加补证或安全任务。原任务、目标、工作流 ID 和预算保留，成功任务不重跑；默认不调用重规划模型，显式开启后仅在规则未追加任务时尝试。Orchestrator 仍默认一次、最多两次 replan。
6. `SynthesisAgent` 默认以规则生成五段报告，保留硬门禁、全部 finding 与任务摘要，不增加模型成本。模型叙述为显式可选增强，不能改变服务端决策或授权操作。
7. 后端持久化 `AgentWorkflowRun` 和 `AgentTaskRun`，模型模式和降级次数进入 spec、observation、agent events 和 metrics，方便后续核对。计划预览和最终报告会说明模型是否真正完成处理。

模型复用 `LLM_*`。初始 Planner 保留原 60 秒默认上限；Observer 的 `WORKFLOW_OBSERVER_TIMEOUT_SECONDS` 默认 20 秒，`WORKFLOW_OBSERVER_MAX_OUTPUT_TOKENS` 默认 1800。仅官方 DeepSeek 的语义复核关闭思考模式，其他调用不变。输入仍受 `WORKFLOW_MODEL_MAX_INPUT_CHARS` 限制，常见密钥字段不进入投影；被截断的事实不作为模型引用依据。

缺密钥、请求错误、超时或非法输出标为 `rules_fallback` 并保留人工复核，不将服务不可用提示混入业务冲突。按策略不调用模型标为 `rules`，不算降级，来源说明明确未做模型复核。没有比较对象也不等于已经证明外部事实正确。

`evidence_count` 是证据条目数量，而 `citations` 是来源定位列表：一个 PR 引用可以对应多个文件和评论，两者不要求 1:1。模型不因为只有摘要或缺少原始日志就凭空扩大任务范围；确定性的覆盖检查仍阻止缺少必要 PR/CI 的合并决策。

这是受约束的中心化工程工作流，不是无限自治系统。源码配置 `WORKFLOW_SYNTHESIS_MODEL_ENABLED=false`、`WORKFLOW_REPLAN_MODEL_ENABLED=false` 是成本与职责策略，不是故障。权限检查和外部操作确认始终由服务端负责。
