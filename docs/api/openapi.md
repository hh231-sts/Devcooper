# Devcooper API

启动后端后访问 `http://localhost:8000/docs` 查看交互式 OpenAPI。

## Core Endpoints

- `POST /api/repos/connect`
- `GET /api/repos`
- `POST /api/repos/{repo_id}/sync`
- `GET /api/repos/{repo_id}/sync-status`
- `GET /api/repos/{repo_id}/issues`
- `POST /api/issues/{issue_id}/analyze`
- `GET /api/issues/{issue_id}/similar`
- `GET /api/repos/{repo_id}/pull-requests`
- `POST /api/pull-requests/{pr_id}/analyze`
- `POST /api/pull-requests/{pr_id}/review-checklist`
- `GET /api/repos/{repo_id}/workflow-runs`
- `GET /api/workflow-runs/{run_id}/failed-jobs`
- `POST /api/workflow-runs/{run_id}/analyze`
- `POST /api/chat`
- `POST /api/search`
- `POST /api/reports/weekly`
- `POST /api/evals/run`

## 后台调查接口

`POST /api/issues/{issue_id}/analyze?conversation_id=...` 提交明确 Issue 目标，立即返回：

```json
{
  "workflow_run_id": "<uuid>",
  "status": "queued",
  "subscription_url": "/api/chat/workflow-runs/<uuid>/events",
  "detail_url": "/api/chat/workflow-runs/<uuid>",
  "answer": "任务已提交，后台执行"
}
```

`POST /api/issues/{issue_id}/analyze/stream` 提交同一类任务，先输出 `submitted`，再订阅共享的持久化事件。断线不取消运行。`GET /api/issues/{issue_id}/analysis-run` 返回最近一次该明确目标的运行编号，供刷新后恢复任务卡；没有记录时返回 `workflow_run_id: null`。

聊天的 `issue_agent.analyze_issue` 和 `workflow.run_engineering_review` 同样返回后台运行编号；计划执行入口使用同一后台模块。

`POST /api/pull-requests/{pr_id}/analyze`、`POST /api/pull-requests/{pr_id}/review-checklist` 和 `POST /api/workflow-runs/{run_id}/analyze` 返回相同提交结构。PR/CI 的 `/analyze/stream` 也先提交再订阅；`GET /api/pull-requests/{pr_id}/analysis-run` 和 `GET /api/workflow-runs/{run_id}/analysis-run` 用于恢复指定对象的最新任务卡。这里的 `/api/workflow-runs/{run_id}` 指原始 CI 运行，`/api/chat/workflow-runs/{id}` 指后台调查运行。

| 接口 | 内容 |
|---|---|
| `GET /api/chat/workflow-runs` | 可按 `repo_id`、`conversation_id` 过滤运行列表，包括历史完成记录 |
| `GET /api/chat/workflow-runs/{id}` | 固定版本、任务预算、当前结果、全部结论版本、证据、协作消息、事件、最终报告和未解决项 |
| `GET /api/chat/workflow-runs/{id}/events?after=<event_id>` | SSE 续读；也支持 `Last-Event-ID` 请求头 |
| `POST /api/chat/workflow-runs/{id}/cancel` | 记录取消请求，当前推进结束后终止余下任务并保存已有结果 |

事件类型为 `review_event`，SSE `id` 是持久化事件编号，数据包含 `kind`、`task_id`、`payload` 和 `created_at`。`settled` 表示流已读完且运行结束；活跃连接可按编号恢复。终态包括 `completed`、`partial`、`failed`、`cancelled`。角色或模型失败的具体原因进入事件与任务错误，不伪造成功响应。
