---
name: pr-review
title: PR 审查
version: 0.2.0
description: 读取固定 base/head SHA 的 diff、实现、调用方、测试与需求，结合 Issue/CI 反馈更新风险和合入建议。
category: github-analysis
entrypoint: pr_review_agent.analyze_pr
tools:
  - pr_review_agent.analyze_pr
  - workflow.run_engineering_review
input_modes:
  - explicit_pr_target
  - immutable_review_snapshot
triggers:
  - pr
  - pull request
  - 合并请求
  - 代码审查
  - 能不能合并
workflow_steps:
  - bind_base_and_head_sha
  - read_diff_implementation_callers_and_tests
  - read_requirement_evidence
  - exchange_issue_and_ci_feedback
  - update_conclusions_and_merge_recommendation
output_contract: InvestigationSubmission -> PRFinish
safety_level: read_only
---

# PR 审查

主 Agent 提交固定目标的审查；跨领域判断使用 `workflow.run_engineering_review`。后台调查按需读取 `references/risk-checklist.md`。

1. 用 `pr.read_diff` 读取 base/head 固定的 diff，用 `code.list` 发现真实路径，用 `code.search`、`code.read` 核查实现、调用方、相关测试。每条证据保留 SHA 和位置。
2. 用 `materials.search`、`materials.read` 读取关联需求。需要未加入的 Issue/CI 角色时，向 Planner 指定本次快照中的明确目标。
3. 用协作工具提出具体问题、答复对方、收到反馈后继续查证。用稳定的结论编号更新兼容性和需求覆盖判断；Observer 补查仍在原任务中推进。
4. `review.finish` 保留 `PRReviewOutput` 字段，增加结论、证据引用和未解决项。P1/P2 进入阻塞项；合入建议须遵守独立 CI 门禁。

不能用「looks good」「没什么大问题」替代审查结论。没有 P1/P2 时也说明证据、P3 建议和人工确认清单。

子 Agent 使用共享运行器的只读材料和协作工具，工具集合不包含自己的分析入口。公开记录由实际调用生成。每角色累计 8 次模型、16 次工具、180 秒；重启保留预算，失败结束受影响任务并交回已有结果。

只读查证，不执行测试、修改文件、评论、批准或合并 PR。证据不足时列出缺口，不制造风险或补写执行记录。
