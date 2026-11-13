# DevFlow AI 的固定评测、后台任务与质量判定

固定评测从 `docs/datas/evals/*.json` 读取用例。`load_eval_suite` 会将每题的 `source_file` 文件名映射到当前仓库已经入库的 Document；没有入库的来源会使评测集的 `ready` 为 false，加载时列出缺失来源并拒绝运行。

评测入口是 `POST /api/evals/rag/run`，必须指定 `repo_id`。`suite_id` 用于选择固定评测集，`cases` 用于直接提交自定义用例。两者不能在同一次请求中提交，同时提交会返回 HTTP 400。固定评测集选择后，不需要再把它的 cases 复制到请求里。

接口先保存 queued 状态及 Eval ID，再通过 FastAPI BackgroundTasks 执行后台任务。客户端使用 `GET /api/evals/{eval_id}` 查询状态。只看到 queued 不代表评测完成。后台任务属于当前应用进程，进程退出后不会自动恢复。

每条用例检查预期来源、必需工具、关键短语、禁用短语和可选的回答正则。随后由 Ragas Collections API 计算 Context Precision、Context Recall、Faithfulness、Answer Relevancy 和 Agent Goal Accuracy。固定 JSON 原始字节的 SHA-256 用于标记同一套题目的版本。

整体质量通过要求硬规则通过、语义评分完整、门槛达标。即使所有硬规则都通过，设置 `run_judge=false` 后，`evaluation_complete=false` 且 `passed=false`；此时只完成部分检查，不能声称完整 RAGAS 质量通过。Judge 缺失、调用失败或指标不完整也不能显示为通过。

核对源码：`backend/app/api/routes/evaluations.py` 的 `run_rag_eval`；`backend/app/services/evals/suites.py` 的 `load_eval_suite`；`backend/app/services/evals/ragas_runner.py` 的 `_finalize_result`。
