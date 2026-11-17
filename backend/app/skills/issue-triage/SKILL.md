---
name: issue-triage
title: Issue 分诊
version: 0.2.0
description: 调查指定 Issue 的需求、验收、重复候选、影响范围与负责人依据，并接收 PR/CI 和 Observer 的定向反馈。
category: github-analysis
entrypoint: issue_agent.analyze_issue
tools:
  - issue_agent.analyze_issue
  - workflow.run_engineering_review
input_modes:
  - explicit_issue_id_or_number
  - explicitly_requested_latest_issue
  - issue_linked_by_target_pr
triggers:
  - issue
  - 工单
  - 分诊
  - 优先级
  - 负责人
workflow_steps:
  - bind_issue_snapshot
  - read_issue_and_candidate_contents
  - investigate_requirements_code_documents_and_team
  - exchange_evidence_and_update_conclusions
  - produce_action_items
output_contract: InvestigationSubmission -> IssueFinish
safety_level: read_only
---

# Issue 分诊

主 Agent 使用 entrypoint 按明确编号或 UUID 提交后台任务，立即交回运行编号。仅用户明确要求“最新 Issue”时选择最新记录；需要三方调查时交给工作流 Planner。

后台角色使用共享调查循环。每次选择一个材料工具，保存真实结果后再作下一次决策：

1. 用 `issue.read` 读取已固定的完整标题、正文、状态、标签、分配人员和更新时间。
2. 用 `materials.search` 发现候选，再用 `materials.read` 读取双方完整内容；有 `next_offset` 时继续读取。检索分数只用于发现候选。
3. 按需用 `code.list` 发现真实文件路径，用 `code.search`、`code.read` 核查固定 SHA 的实现与测试；用材料工具读取项目文档和相关会话，用 `team.read` 获取项目团队画像。
4. 负责人来自已读取的团队画像或已分配人员，无合适人选时保留“待分配”。缺少复现、影响或团队信息时列出待用户补充项。
5. 用 `review.conclude` 保存带证据编号的稳定结论。用 `collaboration.request`、`collaboration.respond` 与 PR/CI 交换具体问题和证据；新增角色由 `planner.request_task` 交给 Planner 绑定目标。
6. 用 `review.finish` 返回 `IssueAnalysisOutput` 业务字段、结构化结论和未解决问题。分诊、优先级、复杂度、负责人、验收条件和草稿均须有明确依据。

后台工具集合由共享运行器提供，不包含 `issue_agent.analyze_issue`。每角色累计 8 次模型、16 次工具、180 秒；重启沿用检查点和预算。失败直接记录原因并交回已有结果，不拼装规则结果冒充完成调查。

只读查证：不能评论、打标签、关闭或分配 Issue，不能修改文件或执行测试。
