# Database Schema

核心表：users、repositories、issues、pull_requests、pr_files、pr_review_comments、workflow_runs、analysis_results、documents、agent_runs、eval_runs。

当前骨架模型位于 `backend/app/db/models.py`。

补充说明：

- `workflow_runs.jobs` 保存 GitHub Actions jobs 和 steps，用于定位失败 job。
- `documents.metadata` 保存标签、文件路径、状态和时间范围，用于 RAG 过滤。
- `eval_runs.result_json` 保存基础评测指标和 Markdown eval report。

## 持久化调查

增量迁移：`backend/app/db/review_migration.py`，随启动在新表建立后执行，可重复执行。

| 表 | 新字段或职责 |
|---|---|
| `pull_requests` | `base_sha`、`head_sha`、`merge_commit_sha` |
| `workflow_runs` | `commit_sha`、`pull_requests_json`、`run_attempt`、`workflow_path` |
| `agent_workflow_runs` | 快照、原任务卡消息、阶段检查点、累计活跃时间、取消标记和运行租约 |
| `agent_task_runs` | 原任务契约、原生模型消息与工具响应、待处理调用、收件箱游标、累计预算、结果版本和任务租约 |
| `review_snapshots` | 固定 PR/CI/Issue 元数据、候选、文档、会话、团队和代码版本 |
| `review_code_archives` | 按快照与 SHA 唯一保存代码归档，读取不依赖可变 checkout |
| `review_evidence` | 真实工具读取的来源、版本、位置和片段 |
| `review_conclusions` | 任务内稳定结论编号及全部版本、证据和关联消息 |
| `review_messages` | 三方请求、答复、Observer 补查与 Planner 新任务请求；运行内去重 |
| `review_events` | 有序持久化执行事件；支持按事件编号继续订阅 |
| `analysis_results` | 新增 `workflow_id`、`task_id`、`result_version`；Issue 最新有效业务结果仍存于此 |

每次推进先提交调用意图，再在事务外调用模型或工具。返回后将检查点、证据、结论、出站消息与事件原子提交。按运行行锁串行分配事件和消息编号，避免并发事务晚提交导致订阅游标跳过事件。运行和任务租约分别保证同一执行单元只能被一个推进者领取。

旧完成记录的新字段为空或使用默认值，原报告仍可查看。活跃运行的快照和归档不会随聊天页面、断线或 worktree 清理删除。
