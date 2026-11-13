# DevFlow AI

DevFlow AI 是一个面向 GitHub PR / Issue 研发协作的全栈 AI Agent 项目。系统会连接仓库数据，分析 Issue、PR diff、CI 日志、项目知识库和本地工作区代码，并返回结构化建议与安全的操作草稿。

## 核心能力

- 连接并同步 GitHub 仓库中的 Issue、Pull Request、PR 文件、Review 评论、Workflow Run、Job 和日志。
- Issue 分析：分类、优先级、复杂度、推荐负责人、重复候选和行动项。
- PR 审查：摘要、关键变更、风险点、检查清单、测试建议和需要重点关注的文件。
- CI 排障：失败类型、可能原因、排查步骤和相关上下文。
- Agent 对话：`ChatAgent` 使用原生工具调用，串联工作区、记忆、RAG、Issue、PR、CI、周报和安全工具。
- 多 Agent 工作流审查：`WorkflowOrchestrator` 构建 Planner -> 专用 Agent -> Observer -> Synthesis 的闭环，用于 PR 就绪度、阻塞项、优先级和跨领域工程决策。
- Skill Runtime：按用户显式指定或触发条件选择 `SKILL.md`，只向当前任务加载完整指令与受限资源；显式 Skill 校验允许工具，多 Agent 中的专用 Agent 分别加载自己的 Skill，并把版本、激活原因、指令摘要和执行校验写入 trace。
- 工作区代码工具：安全地列出文件、读取文件和搜索本地代码。
- RAG 聚焦 Issue 描述、PR 说明与 Review 评论、失败 CI 日志、项目文档、上传知识和已批准的长期记忆；当前代码走工作区/词法检索，团队与实时状态走结构化查询。
- 生成工程周报。
- 基础 Eval 覆盖 Issue 分类、PR 覆盖度、RAG 命中率、工具 schema 就绪度和结构化输出校验。

## 多 Agent 模型链路

配置 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL` 后，初始 Planner 使用模型，Observer 按需做局部语义复核；综合与重规划默认优先采用规则。默认复用 DeepSeek 配置。不要把真实密钥写入源码、测试或发布包。

- Planner 根据用户目标提出结构化任务选择、依赖和补充验收条件，能处理否定和条件表达；服务端校验允许的任务类型、目标可用性、依赖缺失、重复和循环，再绑定当前仓库的 claim。原关键词规划仅作为明确标记的降级路径。
- 有限 replan 先按规则补齐明确缺口，只允许追加新任务，保留已确认任务与 claim，成功任务不重跑。默认不调用模型；显式开启 `WORKFLOW_REPLAN_MODEL_ENABLED` 后，仅规则没有追加任务时尝试模型补充。重规划仍默认最多一次、上限两次。
- Observer 先检查硬门禁、任务覆盖、证据缺口与人工确认。规则已发现问题时不再追加模型调用；否则比较已提供事实的具体矛盾，不做无来源的外部真实性审计。每个模型 finding 必须引用两个不同事实及可逐字校验的原文，不能因引用只有定位信息、可选输出为空或证据数不等于引用数就告警。
- Observer 独立等待上限默认 20 秒、输出上限 1800 tokens；官方 DeepSeek 接口使用非思考模式，其他兼容接口不发送 DeepSeek 专有参数。模型不可用记录在 `model_review`，要求人工复核，不伪装成业务冲突，也不伪装成已完成语义检查。原文引用校验不等于逻辑正确性证明，置信度也不是校准概率。
- Synthesis 默认以规则保留决策、完整摘要、finding 及五段报告，不额外调用模型。显式开启 `WORKFLOW_SYNTHESIS_MODEL_ENABLED` 才增加受引用和决策代码约束的模型叙述；任何模式都不能授权合并、发布或外部写入。
- Orchestrator 仍由程序按依赖调度，负责并发、超时、结果收集和重规划次数，不使用模型决定执行机制。

处理来源区分 `llm`（有效模型输出）、`rules`（按策略使用规则，非故障）和 `rules_fallback`（模型失败或不可用）。有意使用规则不会增加 `model_fallback_count`；初始规划与最后重规划的模式分别保存，避免把规则重规划误称为初始规划未用模型。

在 `backend` 目录运行离线回归：

```bash
python -m pytest app/tests/test_workflow_models.py app/tests/test_workflow_orchestrator.py -q
```

真实模型测试使用隔离的测试数据，不连接真实 GitHub 仓库、不执行外部写操作，但会产生模型费用。配置自己的 `LLM_*`，并显式设置 `DEVFLOW_RUN_LIVE_LLM=1` 后运行：

```bash
python -m pytest app/tests/test_workflow_live_models.py -v
```

这些测试覆盖自然语言规划、语义冲突、规则重规划，以及模型 Planner、真实 PR/CI 分析和规则综合的完整链路。离线 `test_workflow_policy.py` 另覆盖默认策略、引用伪造、超时取消、字段契约与故障时人工复核。它们不等于对任意自然语言或生产仓库的正确性保证。

## 技术栈

- 后端：Python、FastAPI、Pydantic、SQLAlchemy、PostgreSQL、Milvus、httpx、LangChain。
- Agent 编排：原生工具调用 + 专用分析 Agent。
- 前端：Next.js、TypeScript、Tailwind CSS、React Query。
- 基础设施：Docker Compose 与 `.env` 配置。

## 架构

```mermaid
flowchart TD
    FE["Next.js 前端"] --> API["FastAPI 后端"]
    API --> DB["PostgreSQL"]
    API --> Milvus["Milvus 向量库"]
    API --> GH["GitHub REST API"]
    API --> LLM["OpenAI 兼容 LLM API"]
    API --> Conv["对话 Agent"]
    Conv --> Workflow["工作流编排器"]
    Workflow --> Planner["规划 Agent"]
    Workflow --> Observer["观察 Agent"]
    Workflow --> Synthesis["综合 Agent"]
    Conv --> Workspace["工作区工具"]
    Conv --> Memory["记忆 / RAG 工具"]
    Conv --> Issue["Issue 分析 Agent"]
    Conv --> PR["PR 审查 Agent"]
    Conv --> CI["CI 排障 Agent"]
    Conv --> Report["周报 Agent"]
    Conv --> Safety["安全 Agent"]
    Conv --> Skills["Skill 注册中心"]
```

## 快速开始

```bash
cp .env.example .env
docker compose up -d postgres etcd minio milvus

cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

另开一个终端：

```bash
cd frontend
npm install
npm run dev
```

默认地址：

- 后端：http://localhost:8000
- API 文档：http://localhost:8000/docs
- 前端：http://localhost:3000

## 仓库同步范围与状态

仓库页的手动同步默认读取 Issue、PR、CI 以及 PR 文件、Review 评论、Job 的全部分页。`POST /api/repos/{repo_id}/sync` 的 `limit` 表示每类主记录的总数上限，省略或传 `null` 为全部分页；达到上限且还有后续记录时会标记为 `partial`。首次接入默认只取少量记录；定时同步默认读取全部分页，也可通过 `AUTO_SYNC_LIMIT` 设置总数上限。所有同步均会报告实际覆盖范围。

同步状态区分 `completed`（所选数据源完整完成）、`empty`（请求成功但为空）、`partial`（数量上限或部分失败）、`failed`（所选主接口全部失败）和 `skipped`（未选择数据源）。响应中的 `sources`、`errors` 和 `/sync-status` 可查看覆盖范围和失败原因；失败时保留已有明细，不更新最近成功同步时间。

仓库健康分析会检查数据是否为空、同步是否完整及是否超过 24 小时。数据不足时明确提示，Observer 阻止据此给出可推进的结论；统计信息不再附带固定置信度。旧数据库缺少可验证的同步记录时，需要重新同步。升级后重启后端，启动流程会自动补充 `repositories.sync_metadata` 字段，保留原有数据。

在 `backend` 目录运行 `python -m pytest app/tests/test_repo_sync_integration.py -q`，可验证真实本地 HTTP 分页、数据库写入、故障处理、健康回答和旧数据库升级。

### 并发同步与评测

同一仓库的手动同步、定时同步和脚本同步共用互斥控制；重复触发时接口返回 `409` 和明确提示，不再排队占用数据库。PostgreSQL 使用会话级 advisory lock，支持多个后端进程；SQLite 演示环境仅提供单进程互斥，应以单后端进程运行。

同步先拉完所选主记录和明细，再在工作线程中用独立 Session、短事务统一写入。远端请求失败仍保留原有明细；发布失败整体回滚。PostgreSQL 写入阶段的锁等待上限为 5 秒，单条语句上限为 30 秒；取消请求时会先等正在执行的数据库操作完成，再释放互斥。

评测每条用例使用当前仓库关系库输入的可丢弃副本，保留来源 UUID 与知识库配置，不复制正式聊天历史或其他仓库。Agent、工具、记忆检索与运行时写入使用该副本；工作区文件和向量召回仍读取现有来源，不代表冻结了外部服务。聊天证据按会话重建，不会删除其他会话或仓库共享证据。后台评测与聊天准备阶段的阻塞操作均不在 API 事件循环上执行。

在 `backend` 目录运行 `python -m pytest app/tests/test_concurrency_regressions.py -q` 可执行 SQLite 并发回归。需要验证真实 PostgreSQL 的锁等待和跨进程互斥时，为独立测试数据库设置 `DEVFLOW_TEST_POSTGRES_URL` 后运行同一命令；测试会创建并清理专属临时 schema，不调用真实模型或 GitHub。

这些后台任务仍在后端进程内运行，并非持久任务队列。正常取消评测会保存中断状态；进程被强制终止时，旧评测仍可能保留 `queued/running`，需确认原进程已退出后重新提交，不能把旧状态当作任务仍在执行。

## RAG 知识问答系统

项目内置了与第 7.1 节流程一一对应的完整 RAG 系统：知识库创建向导、文档解析、段落感知切片、Embedding、向量/关键词混合检索、重排、召回测试、问答工作室、上下文组装、基于证据生成和编号引用。支持 TXT、Markdown、PDF、DOCX、JSON、CSV 与日志文件；重复文件会按 SHA-256 跳过。

对话链路采用混合 RAG：`ContextAssembler` 会在调用 ChatAgent 前按原始问题检索并注入证据；进入推理循环后，模型仍可把 `rag.search_similar_documents` 当作工具，改写查询、连续补查不同资料，并根据多次工具结果交叉验证答案。

项目不会把所有私域数据都向量化。只有需要跨来源语义召回、会反复使用的非结构化知识进入 RAG；源码与配置以当前 checkout 为准，优先通过 `workspace.search_code` 和 `workspace.read_file` 检索；Issue、PR、CI 的状态字段、团队成员和周报等结构化或派生数据直接查数据库/API。RAG 返回的是候选证据，涉及实时状态和当前代码时仍要回到权威数据源核验。

从旧版本升级后，请对每个知识库调用一次 `POST /api/rag/{repo_id}/reindex`。重建会按新边界清理历史向量和旧会话 Document；只部署代码不会主动删除已有索引。

如果只想先验证页面、结构化数据和无模型兜底回答，Windows 本地可以用下面的脚本启动 SQLite 演示环境：

```powershell
.\scripts\start_rag.ps1
```

脚本会关闭 Milvus，使用 deterministic embedding 和启发式重排，并停用演示知识库的 `0.8` 分数阈值，避免只有关键词分支时全部候选都被过滤。因此它可以验证页面、上传解析、关系库存储、Hybrid 的关键词分支和引用展示，但向量检索状态会明确显示 `degraded`，不能用来证明完整 RAG 已经跑通。要验证向量召回、混合检索和重建索引，仍需先按“快速开始”启动 PostgreSQL、etcd、MinIO 和 Milvus。

知识库默认选择 `Qwen/Qwen3-Embedding-0.6B` 与 `qwen3-rerank`。安装 `backend/requirements-local-embedding.txt` 后可运行本地 Qwen Embedding；配置 `DASHSCOPE_API_KEY`、`RERANK_BASE_URL` 后可切换百炼 `text-embedding-v4` 与 `qwen3-rerank`。默认 Embedding 依赖缺失时系统会明确报错，不会在生产配置下静默改用另一套向量空间；只有上面的本地演示脚本会显式选择 deterministic embedding。已经配置 `LLM_API_KEY` 时，可用 `.\scripts\start_rag.ps1 -UseConfiguredLLM` 启用模型生成。

RAG API：

```text
POST /api/rag/knowledge-bases
GET  /api/rag/knowledge-bases
GET  /api/rag/knowledge-bases/{repo_id}
POST /api/rag/knowledge-bases/{repo_id}/documents
POST /api/rag/knowledge-bases/{repo_id}/retrieval-tests
GET  /api/rag/{repo_id}/status
POST /api/rag/{repo_id}/search
POST /api/rag/{repo_id}/ask
POST /api/rag/{repo_id}/reindex
```

## ChatAgent RAGAS 评测

完整源码包已附带第 15.2 节的 5 条固定评测题和 4 份配套知识文档，不需要另找第 15 章下载包。先在解压后的 `DevFlow-AI` 根目录运行 `python scripts/prepare_eval_data.py --check` 核对文件；后端就绪后，使用后端虚拟环境的 Python 运行 `scripts/prepare_eval_data.py --demo-repo` 导入知识并确认套件 `ready=true`。已有仓库可以改用 `--repo-id <仓库UUID>`。安装、导入、运行和失败排查的完整步骤见 [固定评测集操作说明](docs/datas/README.md)。

启动主应用后打开 `http://127.0.0.1:3000/evals`，选择仓库、固定评测集和 Top-K，再点击“运行固定 RAGAS 评测”。评测会在隔离会话中真实运行生产 ChatAgent 和工具集，收集预检索与 Agent 二次检索证据，然后使用 Ragas 0.4 Collections API 计算 Context Precision、Context Recall、Faithfulness、Answer Relevancy 和 Agent Goal Accuracy。结果会保存评测集 Hash、阈值、逐题工具轨迹、检索证据、硬规则与失败诊断，便于对同一基线做回归。

默认 Judge 复用 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`；也可以通过 `.env` 中的 `RAGAS_JUDGE_API_KEY`、`RAGAS_JUDGE_BASE_URL`、`RAGAS_JUDGE_MODEL` 单独配置。课程固定基线位于 `docs/datas/evals/rag_cases.json`，套件列表和加载器会检查用例引用的文档是否已进入当前仓库知识库。它不是外部盲测集；新包附带的题目是针对当前代码整理的回归版本：

```http
POST /api/evals/rag/run
Content-Type: application/json

{
  "repo_id": "<repository-uuid>",
  "run_judge": true,
  "top_k": 5,
  "suite_id": "devflow-real-project-rag-baseline"
}
```

提交接口会立即返回 `queued` 状态和 `eval_id`，ChatAgent 与 RAGAS 在后台继续运行。通过 `GET /api/evals/{eval_id}` 轮询 `result.progress`；状态变为 `passed`、`failed`、`error` 或 `no_cases` 时结束。网页会自动轮询，刷新或离开页面不会中断已经提交的任务。

整体通过要求硬规则全部满足，并且五项语义指标都达到 `.env` 中对应的 `RAGAS_*_MIN` 门槛。页面把“执行状态”和“质量状态”分开显示：低分是质量失败；Judge 调用失败或指标缺失是评测不完整，不能伪装成质量结论。

评测失败后可以直接在同一页面继续处理：

- 展开 Case 查看实际回答、参考答案、检索来源、工具轨迹和可执行诊断；
- 对 Judge 超时或缺失指标点击“仅重试异常评分”，复用已经保存的回答和证据，不重新运行 ChatAgent；
- 选择两个相同评测集 Hash、Ragas/Judge 配置和 Top-K 的结果做回归对比；
- 生成 `create_issue` 类型的 ActionDraft，把失败 Case、证据和建议带入人工确认流程，不会直接写入 GitHub。

## 历史工具结果微压缩

`MessageStore.context_projection()` 从当前活动消息关联的 `ChatSession` 工具事件生成历史视图，兼容消息元数据中的旧事件和独立工具消息。`ContextAssembler` 先按压力微压缩，再分配近期历史预算；原始消息和封存记录不回写、不删除。

微压缩只处理离开近期保护区的可恢复长工具结果，保留来源、调用 ID、相关摘录和回读参数。普通助手回答不会被当作工具结果处理。正式摘要与压力估算也包含工具结果，摘要边界之前未保留的工具记录不会重新注入。

在 `backend` 目录运行 `python -m pytest app/tests/test_history_microcompact.py -q`，可验证普通与流式聊天跨轮接入、保护条件、会话隔离、摘要边界、无模型微压缩和原始记录回读。测试使用脚本模型与内存数据库，不调用付费模型。
