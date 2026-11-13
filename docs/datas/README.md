# 第 15.2 节：固定评测集与配套知识

2026-10-23 固定评测集补齐版完整源码包已经包含本目录，不需要另找一个第 15 章 ZIP。普通版和详细注释版使用相同的数据与导入脚本。此前的依赖修复和历史工具微压缩修复均已保留。

这套题目是针对当前交付代码人工整理的课程回归基线，不是从缺失目录中恢复的旧文件，也不是外部盲测集。它用 5 条固定题目检查来源核验、专用 Agent、上下文装配、评测输入和不完整评分判定。

## 包内文件

- `evals/rag_cases.json`：固定题目、参考答案、预期来源文件和硬规则。套件 ID 为 `devflow-real-project-rag-baseline`。
- `knowledge_base/01_project_overview.md`：项目定位与 Issue、PR、CI 专用 Agent。
- `knowledge_base/02_rag_pipeline.md`：ContextAssembler、混合 RAG 和隔离评测会话。
- `knowledge_base/03_eval_workflow.md`：评测请求、后台任务与质量判定。
- `knowledge_base/04_data_flow_and_sources.md`：当前 Git checkout、工作区工具和知识来源分工。
- `../../scripts/prepare_eval_data.py`：校验数据，通过正式知识上传 API 导入，再确认套件来源就绪和 SHA-256 一致。

**只上传 `knowledge_base` 中的四份 Markdown。不要把 `rag_cases.json` 或本说明上传为知识文档。** 题目和参考答案留给评测器使用；四份知识文档才是被检索的材料。

## 先检查解压内容

进入 ZIP 解压后的 `DevFlow-AI` 根目录，也就是同时包含 `backend`、`frontend`、`scripts`、`docs` 的目录，运行：

```powershell
python scripts/prepare_eval_data.py --check
```

预期输出 `Dataset OK: 5 cases, 4 knowledge files` 和本地套件 SHA-256。这个检查只读本地文件，不需要安装后端依赖、不访问网络、不调用模型，也不代表 RAG 质量已经通过。

## 导入到正在运行的项目

先按项目根目录 `README.md` 的快速开始安装并启动后端。在项目根目录的 `.env` 中配置可用的 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`；Judge 默认复用这套配置，也可以使用 `RAGAS_JUDGE_*` 独立配置。完整语义检索还需要可用的 Embedding 和 Milvus。使用默认本地 Qwen Embedding 时，还要安装 `backend/requirements-local-embedding.txt` 并准备模型。

从项目根目录使用安装了后端依赖的同一个 Python 运行脚本。Windows 示例：

```powershell
.\backend\.venv\Scripts\python.exe scripts/prepare_eval_data.py --demo-repo
```

`--demo-repo` 创建或复用 `course-demo/devflow-eval-course` 这个本地仓库记录，不验证或同步 GitHub，不要求这个名称对应真实远端仓库。它不会更改已有真实仓库的配置。做这个独立课程练习时，建议在 `.env` 设置 `AUTO_SYNC_ENABLED=false` 后重启后端，避免后台定时同步演示记录。

如果希望使用自己已连接的仓库，改为明确指定它的 UUID。二者只选一个：

```powershell
.\backend\.venv\Scripts\python.exe scripts/prepare_eval_data.py --repo-id "替换为仓库UUID"
```

脚本会上传四份知识文档，输出仓库 UUID，并确认固定套件 `ready=true`、`missing_sources=[]`、套件 SHA-256 与本地文件相同。重复执行时相同内容由上传 API 去重。发现同名但内容不同的旧来源时，脚本拒绝混用；请换一个干净的练习仓库，或在界面中明确删除旧来源后再导入。

后端不是 8000 端口时，加上 `--api-base-url http://127.0.0.1:实际端口`。如果提示找不到套件或 Hash 不同，请确认后端是从这次新包启动的，而不是仍在运行旧目录里的服务。

## 执行固定评测

前端启动后打开 `http://127.0.0.1:3000/evals`，选择刚才导入的仓库和「DevFlow AI 固定知识问答基线（2026-10-23）」，再运行固定评测。也可以直接通过脚本提交并等待最终结果：

```powershell
.\backend\.venv\Scripts\python.exe scripts/prepare_eval_data.py --demo-repo --run --output .data/eval-course-result.json
```

这条命令会真实调用当前配置的 ChatAgent 模型与 Judge，可能产生模型费用。脚本默认最多等待 1800 秒；超时后保留 Eval ID，可以在界面或 `GET /api/evals/{eval_id}` 查询原任务，不要反复提交。

对应的评测请求是：

```http
POST /api/evals/rag/run
Content-Type: application/json

{
  "name": "course-fixed-rag-eval",
  "repo_id": "替换为脚本输出的仓库UUID",
  "suite_id": "devflow-real-project-rag-baseline",
  "top_k": 5,
  "run_judge": true
}
```

不要同时传 `cases`。先收到 `queued` 只是提交成功，必须等后台任务结束。报告应包含同一个套件 Hash、5 条逐题现场、检索证据、硬规则与 RAGAS 指标。执行完成和质量通过是两件事：只有 `evaluation_complete=true` 且 `passed=true` 才是完整质量通过；固定题目不保证所有模型或检索参数都达标，低分和格式失败需要按逐题报告分析，不能通过降低门槛伪装成跑通。

## 仅检查基础链路

没有可用模型、Milvus 或 Embedding 时，可以先运行本地文件检查和离线回归：

```powershell
python scripts/prepare_eval_data.py --check
cd backend
python -m pytest app/tests/test_eval_dataset.py app/tests/test_eval_runner.py -q
```

项目已有的 `scripts/start_rag.ps1` 提供 SQLite、deterministic Embedding 和关闭 Milvus 的演示环境。演示后端就绪后，可以另开终端，在项目根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe scripts/prepare_eval_data.py --demo-repo --run --without-judge --output .data/eval-course-partial.json
```

关闭 Judge 只能检查数据导入、固定套件和后台调用链，脚本会返回非零退出码 `2`，因为完整质量没有通过。它可能得到 `partial` 或硬规则失败；不会把缺模型的兜底答案、关键词检索或未计算的语义指标包装成完整 RAGAS 验收。正常完整质量通过的退出码为 `0`，文件、接口或超时错误为 `1`，完成执行但质量未通过为 `2`。
