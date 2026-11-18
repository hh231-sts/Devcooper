---
name: ci-debug
title: CI 排障
version: 0.2.0
description: 调查指定 CI attempt 的 jobs、日志、失败测试、workflow 和依赖，区分失败原因、与 PR 的关系及合并阻塞判断。
category: github-analysis
entrypoint: ci_debug_agent.analyze_workflow_run
tools:
  - ci_debug_agent.analyze_workflow_run
  - workflow.run_engineering_review
input_modes:
  - explicit_ci_run
  - latest_failed_run_linked_to_pr_version
triggers:
  - ci
  - workflow
  - github actions
  - 流水线
  - 构建失败
  - 测试失败
workflow_steps:
  - bind_run_commit_pr_and_attempt
  - read_jobs_logs_and_failed_tests
  - inspect_fixed_workflow_and_dependencies
  - respond_to_issue_and_pr_questions
  - report_cause_relationship_and_gate
output_contract: InvestigationSubmission -> CIFinish
safety_level: read_only
---

# CI 排障

主 Agent 提交指定运行。未指定时，由协调器选择与目标 PR head 或明确关联的合成合并版本匹配的最新失败运行；没有匹配材料就报告缺口。

1. 用 `ci.read_jobs`、`ci.read_logs`、`ci.read_failed_tests` 读取固定 attempt 的材料，优先定位最早错误。
2. 用 `code.list` 发现固定 SHA 中真实存在的文件路径，再读取测试源文件、workflow 与依赖。PR head 和 CI commit 分别记录，保持合成合并关系可见。
3. 用协作工具回答 Issue/PR 的具体问题；收到答复或 Observer 补查时，在原任务更新结论版本。
4. `review.finish` 保留 `CIDebugOutput`，分开提供 `failure_cause`、`pr_relationship` 与 `is_merge_blocking`，并给出结论、证据引用和未解决项。无法证实的因果关系写为疑点；失败 CI 的门禁由服务端单独保留。

子 Agent 工具集合不包含自己的分析入口。公开记录来自实际调用；每角色累计 8 次模型、16 次工具、180 秒。重启不重置预算；调用失败结束受影响任务，不重试或补写证据。

只读查证，不执行测试、改文件、push、评论或重新运行 workflow。
