# DevFlow AI 的项目定位与专用 Agent

DevFlow AI 是面向 GitHub 研发协作的全栈 AI Agent 项目，后端使用 FastAPI，前端使用 Next.js。它把仓库事实、项目知识和受限工具提供给 Agent，输出分析建议和需要人工确认的操作草稿。

项目中的三类专用分析 Agent 分别分析 Issue、PR、CI。Issue 分析关注问题分类、优先级、复杂度和行动项；PR 分析关注代码变更、风险与测试建议；CI 分析关注失败日志、可能原因和排查步骤。

ChatAgent 负责用户对话，可以调用这些专用能力。多 Agent 工作流由 WorkflowOrchestrator 按依赖调度，并使用 Planner、Observer 和 Synthesis 形成规划、检查和综合的处理链。分析结果不会直接授权合并、发布或外部写操作。

核对源码：`README.md`；`backend/app/services/agents/issue_agent.py`、`pr_review_agent.py`、`ci_debug_agent.py`。
