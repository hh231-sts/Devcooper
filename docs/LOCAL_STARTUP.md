# 本地启动与部署说明

本文档对应 Windows 本机环境，项目根目录指包含 `backend`、`frontend`、`scripts` 的 `Devcooper` 目录。

## 前置条件

- Docker Desktop（PostgreSQL、etcd、MinIO、Milvus）
- Python 3.12（项目使用 `backend/.venv`）
- Node.js 18+（项目已在 Node 24 上验证）
- `.env` 已配置 `LLM_API_KEY`（DeepSeek）和 `GITHUB_TOKEN`

`.env` 中的默认端口已经按本机情况调整：

- 后端：`8001`
- 前端：`4000`（本机 3000/3001/3002 属于 Windows 保留端口，无法监听）
- PostgreSQL 宿主端口：`15432`

## 首次安装

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-local-embedding.txt

cd ..\frontend
npm install

cd ..
.\scripts\download_embedding_model.cmd
```

`download_embedding_model.ps1` 默认从 `hf-mirror.com` 下载 `Qwen/Qwen3-Embedding-0.6B` 到 `.data/hf-model`（约 1.1 GB）。由于 `huggingface.co` 在国内通常不可达，启动脚本会把 `HF_HOME` 指向该目录并启用离线模式，避免后端在请求时卡在联网检查上。

## 启动

在项目根目录打开普通 PowerShell 窗口：

```powershell
.\scripts\start_dev.cmd
```

`.cmd` 包装器会用进程级 `-ExecutionPolicy Bypass` 调用 PowerShell，不需要修改系统的执行策略。也可以直接运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_dev.ps1
```

脚本会依次完成：

1. 检查 `.env`、Python 虚拟环境、前端依赖和本地 Embedding 模型。
2. 启动 Docker Desktop（如果未运行），并执行 `docker compose up -d postgres etcd minio milvus`。
3. 等待 PostgreSQL 健康检查和 Milvus 端口就绪。
4. 启动后端 `uvicorn app.main:app --host 127.0.0.1 --port 8001`。
5. 启动前端 `next dev -p 4000`。
6. 等待健康检查并输出地址、日志目录和停止命令。

启动地址：

- 前端：http://localhost:4000
- 后端：http://localhost:8001
- API 文档：http://localhost:8001/docs

日志位于 `.data/logs/`，进程 PID 位于 `.data/run/`。

## 停止

```powershell
.\scripts\stop_dev.cmd
```

只停后端或只停前端：

```powershell
.\scripts\stop_dev.cmd -BackendOnly
.\scripts\stop_dev.cmd -FrontendOnly
```

## 可选参数

```powershell
# 不检查/启动 Docker（使用外部 PostgreSQL 和 Milvus 时）
.\scripts\start_dev.cmd -SkipDocker

# 只启动后端
.\scripts\start_dev.cmd -SkipFrontend

# 换端口
.\scripts\start_dev.cmd -BackendPort 8002 -FrontendPort 4001
```

## 常见问题

- **后端启动后第一次 RAG 或聊天较慢**：本地 Qwen Embedding 首次加载约 10 秒，之后会复用缓存。
- **RAG 状态显示 rerank unavailable**：没有配置百炼 `DASHSCOPE_API_KEY` 和 `RERANK_BASE_URL`，系统会使用本地启发式重排，检索仍可用。要启用 `qwen3-rerank`，在 `.env` 中补充这两项后重启后端。
- **向量检索为空**：在知识库页面或调用 `POST /api/rag/{repo_id}/reindex` 重建索引。重建会按当前 Embedding 重新写入 Milvus。
- **仓库同步显示 partial**：同步请求带了 `limit`，或部分数据源失败。需要全量同步时，省略 `limit` 或传 `null`；大仓库全量同步可能耗时较久。
- **不要提交 `.env`**：其中包含 DeepSeek Key 和 GitHub Token，`.gitignore` 已排除。

## 当前验证结果

- 后端健康检查：`GET /health` 返回 `{"status":"ok"}`。
- 前端页面：`/`、`/chat`、`/rag`、`/repos`、`/evals` 等路由均返回 200。
- RAG：Qwen3-Embedding-0.6B 本地加载成功，Milvus 向量召回与混合检索可用。
- LLM：`deepseek-flash` 生成成功，RAG 问答与 ChatAgent 均返回模型答案。
- GitHub：`GITHUB_TOKEN` 对应 `hh231-sts`，仓库同步接口可用。
