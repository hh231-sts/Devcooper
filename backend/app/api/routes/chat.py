import uuid
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.models import ActionDraft, AgentRun, AgentTaskRun, AgentWorkflowRun, ChatMessage, Conversation, Issue, PullRequest, Repository, WorkflowRun
from app.db.concurrency import run_blocking
from app.db.session import get_db
from app.schemas.chat import ChatHistoryMessage, ChatPlanExecuteRequest, ChatRequest, ChatResponse, PromptRecommendationRequest, PromptRecommendationResponse
from app.services.agents.ci_debug_agent import CIDebugAgent
from app.services.agents.chat_agent import ChatAgent, encode_sse
from app.services.agents.issue_agent import IssueAgent
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.workflow_models import execution_label
from app.services.agents.pr_review_agent import PRReviewAgent
from app.services.agents.recommendation_agent import RecommendationAgent
from app.services.agents.report_agent import ReportAgent
from app.services.agents.safety_agent import SafetyAgent
from app.services.agents.workflow_orchestrator import WorkflowOrchestrator
from app.services.chat_memory import ContextAssembler, EvidenceStore, MemoryTools, MessageStore, ProgressiveContextManager, SessionSealer
from app.services.code_search import query_terms, search_checkout_code
from app.services.conversations import ensure_conversation, touch_conversation
from app.services.memory_hub import record_recall_event
from app.services.mcp_client import StdioMcpClient
from app.services.rag.retrieval import search_similar_documents
from app.services.rag.vector_store import add_document
from app.schemas.workflow import WorkflowSpec, WorkflowTask, WorkflowTaskResult
from app.services.skills import SkillRegistry, get_skill_registry
from app.services.permissions import ensure_demo_user, write_audit_log
from app.services.repository_health import repository_health_snapshot

router = APIRouter()


def _append_assistant_response(store: MessageStore, db: Session, **kwargs) -> ChatMessage:
    """Write the response onto its already-persisted review card when one was submitted."""
    calls = kwargs.get("tool_calls", [])
    run_id = next(((call.get("arguments") or {}).get("workflow_run_id") for call in calls if (call.get("arguments") or {}).get("workflow_run_id")), None)
    if run_id:
        # Serialize chat persistence with the worker's final-report write and refresh cached ORM state.
        workflow = db.query(AgentWorkflowRun).filter_by(id=uuid.UUID(run_id)).populate_existing().with_for_update().one_or_none()
        card = db.get(ChatMessage, workflow.assistant_message_id, populate_existing=True) if workflow and workflow.assistant_message_id else None
        if card:
            card.content = workflow.final_answer or kwargs["content"]
            card.tool_calls = calls
            card.meta = {**(card.meta or {}), **kwargs.get("meta", {}), "workflow_run_id": run_id, "workflow_status": workflow.status}
            db.flush()
            return card
    return store.append(**kwargs)


async def _seal_chat_turn(db: Session, **kwargs) -> None:
    # The pending card and its transcript are already durable. Its final report arrives from the worker.
    if any((call.get("arguments") or {}).get("workflow_run_id") for call in kwargs.get("tool_calls", [])):
        return
    await SessionSealer(db).seal(**kwargs)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
BACKEND_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".next",
    ".nuxt",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
WORKSPACE_SECRET_FILENAMES = {".env", ".env.local", ".env.production", ".npmrc", ".pypirc"}
WORKSPACE_TEXT_FILENAMES = {"Dockerfile", "Makefile", "README", "LICENSE", "package.json", "requirements.txt", "pyproject.toml"}
WORKSPACE_TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".md",
    ".mdx",
    ".txt",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".html",
    ".css",
    ".scss",
}


@router.get("/workflow-runs")
async def list_workflow_runs(
    repo_id: uuid.UUID | None = Query(default=None),
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentWorkflowRun)
    if repo_id is not None:
        query = query.filter(AgentWorkflowRun.repo_id == repo_id)
    if conversation_id is not None:
        query = query.filter(AgentWorkflowRun.conversation_id == conversation_id)
    rows = query.order_by(AgentWorkflowRun.created_at.desc()).limit(limit).all()
    workflow_ids = [row.id for row in rows]
    tasks = (
        db.query(AgentTaskRun)
        .filter(AgentTaskRun.workflow_id.in_(workflow_ids))
        .order_by(AgentTaskRun.started_at.asc(), AgentTaskRun.task_id.asc())
        .all()
        if workflow_ids
        else []
    )
    tasks_by_workflow: dict[uuid.UUID, list[AgentTaskRun]] = {}
    for task in tasks:
        tasks_by_workflow.setdefault(task.workflow_id, []).append(task)
    return [
        {
            "id": str(row.id),
            "repo_id": str(row.repo_id) if row.repo_id else None,
            "conversation_id": str(row.conversation_id) if row.conversation_id else None,
            "goal": row.goal,
            "status": row.status,
            "snapshot_id": str(row.snapshot_id) if row.snapshot_id else None,
            "spec": row.spec_json or {},
            "observation": row.observation_json or {},
            "final_answer": row.final_answer,
            "metrics": row.metrics or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "tasks": [
                {
                    "id": str(task.id),
                    "task_id": task.task_id,
                    "agent_name": task.agent_name,
                    "task_type": task.task_type,
                    "status": task.status,
                    "summary": str((task.result_json or {}).get("summary") or ""),
                    "error": task.error,
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                }
                for task in tasks_by_workflow.get(row.id, [])
            ],
        }
        for row in rows
    ]


@router.get("/history", response_model=list[ChatHistoryMessage])
async def chat_history(
    repo_id: uuid.UUID = Query(...),
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    before_message_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _repo_or_404(db, repo_id)
    conversation = ensure_conversation(db, repo_id, conversation_id)
    conversation_id = conversation.id
    db.commit()
    rows = [
        item
        for item in MessageStore(db).timeline(
            repo_id,
            conversation_id,
            limit=limit,
            before_message_id=before_message_id,
        )
        if item.role in {"user", "assistant"}
    ]
    return [
        {
            "id": str(item.id),
            "conversation_id": str(item.conversation_id) if item.conversation_id else None,
            "role": item.role,
            "content": item.content,
            "route": item.route,
            "tool_calls": item.tool_calls or [],
            "agent_events": (item.meta or {}).get("agent_events", []),
            "workflow_run_id": (item.meta or {}).get("workflow_run_id"),
            "compression_stats": (item.meta or {}).get("compression_stats", {}),
            "progressive_compaction": (item.meta or {}).get("progressive_compaction", {}),
            "model_usage": (item.meta or {}).get("model_usage", []),
            "context_diagnostics": (item.meta or {}).get("context_diagnostics", {}),
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in rows
    ]


def _repo_or_404(db: Session, repo_id: uuid.UUID) -> Repository:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到指定仓库。")
    return repo


def _client_context(value: dict[str, Any] | None) -> dict[str, Any]:
    server_owned = {
        "repo_id",
        "conversation_id",
        "tools",
        "server_context",
        "server_messages",
        "evidence",
        "compression_stats",
        "progressive_compaction",
        "registered_skills",
        "skill_description_by_tool",
    }
    return {key: item for key, item in (value or {}).items() if key not in server_owned}


def _item_time(item: Any) -> datetime:
    return item.updated_at or item.created_at or datetime.min


def _join_items(items: list[str], empty_text: str) -> str:
    return "; ".join(items) if items else empty_text


def _source_type_from_message(message: str) -> str | None:
    lowered = message.lower()
    if "issue" in lowered:
        return "issue"
    if "pr" in lowered or "pull request" in lowered or "diff" in lowered:
        return "pull_request"
    if "ci" in lowered or "workflow" in lowered:
        return "workflow_run"
    if any(token in lowered for token in ["knowledge", "知识库", "上传资料", "上传文档"]):
        return "knowledge_file"
    return None


def _risky_action(message: str) -> str | None:
    lowered = message.lower()
    if any(token in lowered for token in ["comment", "reply", "post"]):
        return "comment"
    if any(token in lowered for token in ["create issue", "new issue"]):
        return "create_issue"
    if any(token in lowered for token in ["send", "send_report"]):
        return "send_report"
    if any(token in lowered for token in ["close issue", "label", "write"]):
        return "write_back"
    return None


def _date_window_from_context(context: dict[str, Any]) -> tuple[datetime, datetime]:
    end_at = datetime.now(timezone.utc)
    time_range = str(context.get("time_range") or "7d").lower()
    days = int(time_range[:-1]) if time_range.endswith("d") and time_range[:-1].isdigit() else 7
    start_at = end_at - timedelta(days=max(1, days) - 1)
    if context.get("start_date"):
        start_at = datetime.fromisoformat(str(context["start_date"])).replace(tzinfo=timezone.utc)
    if context.get("end_date"):
        end_at = datetime.fromisoformat(str(context["end_date"])).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    return start_at, end_at


def _within(value: datetime | None, start_at: datetime, end_at: datetime) -> bool:
    if not value:
        return False
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return start_at <= current <= end_at


def _repo_payload(repo_id: uuid.UUID, db: Session) -> tuple[list[Issue], list[PullRequest], list[WorkflowRun]]:
    issues = db.query(Issue).filter(Issue.repo_id == repo_id).all()
    prs = (
        db.query(PullRequest)
        .options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments))
        .filter(PullRequest.repo_id == repo_id)
        .all()
    )
    runs = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_id).all()
    return issues, prs, runs


def _recommendation_payload(
    repo: Repository | None,
    issues: list[Issue],
    prs: list[PullRequest],
    runs: list[WorkflowRun],
    exclude: list[str],
    limit: int,
) -> dict[str, Any]:
    return {
        "repo": {"id": str(repo.id), "full_name": repo.full_name} if repo else None,
        "issues": [
            {
                "id": str(item.id),
                "number": item.number,
                "title": item.title,
                "state": item.state,
                "labels": item.labels or [],
                "assignees": item.assignees or [],
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "closed_at": item.closed_at,
            }
            for item in issues
        ],
        "pull_requests": [
            {
                "id": str(item.id),
                "number": item.number,
                "title": item.title,
                "state": item.state,
                "author": item.author,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "merged_at": item.merged_at,
            }
            for item in prs
        ],
        "workflow_runs": [
            {
                "id": str(item.id),
                "name": item.name,
                "status": item.status,
                "conclusion": item.conclusion,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in runs
        ],
        "exclude": exclude,
        "limit": limit,
    }


def _repo_health_answer(repo: Repository, issues: list[Issue], prs: list[PullRequest], runs: list[WorkflowRun]) -> str:
    snapshot = repository_health_snapshot(repo, issues, prs, runs)
    notice = f"当前仓库：{repo.full_name}\n\n数据说明：{snapshot['message']}"
    if not snapshot["evidence_count"]:
        return notice
    open_issues = [item for item in issues if item.state == "open"]
    open_prs = [item for item in prs if item.state == "open"]
    merged_prs = [item for item in prs if item.merged_at]
    failed_runs = [item for item in runs if item.conclusion == "failure"]
    latest_issues = sorted(open_issues, key=_item_time, reverse=True)[:3]
    latest_prs = sorted(prs, key=_item_time, reverse=True)[:3]
    issue_titles = _join_items([f"#{item.number} {item.title}" for item in latest_issues], "没有打开的 Issue")
    pr_titles = _join_items([f"#{item.number} {item.title}" for item in latest_prs], "没有 PR 记录")
    ci_text = f"已同步 {len(failed_runs)} 次失败 CI 运行" if failed_runs else "还没有同步到失败 CI 运行"
    return (
        f"{notice}\n\n"
        f"- Issues：{len(open_issues)} 个打开。近期关注：{issue_titles}。\n"
        f"- PRs：{len(open_prs)} 个打开，{len(merged_prs)} 个已合并。近期 PR：{pr_titles}。\n"
        f"- CI：{ci_text}。\n\n"
        "建议下一步：先看最新打开的 Issue，或检查最新 PR 风险；如果同步后出现失败 CI，可以让我继续排障。"
    )


def _semantic_search(repo: Repository, message: str, context: dict[str, Any], db: Session) -> tuple[str, list[dict[str, str]], list[dict[str, Any]]]:
    filters = {
        "label": context.get("label"),
        "path": context.get("path"),
        "module": context.get("module"),
        "state": context.get("state"),
        "start_date": context.get("start_date"),
        "end_date": context.get("end_date"),
    }
    results = search_similar_documents(
        db,
        repo.id,
        message,
        source_type=context.get("source_type") or _source_type_from_message(message),
        limit=int(context.get("limit", 5)),
        metadata_filters={key: value for key, value in filters.items() if value},
    )
    if not results:
        return "没有找到足够相似的历史记录。可以尝试放宽标签、路径或时间过滤条件。", [], []
    lines = ["找到以下相关历史记录："]
    citations: list[dict[str, str]] = []
    for index, item in enumerate(results, start=1):
        meta = item.get("metadata") or {}
        number = meta.get("number")
        prefix = f"#{number} " if number else ""
        lines.append(f"- [{index}] {item['source_type']} {prefix}{item['title']}：相似度 {item['score']}")
        snippet = str(item.get("snippet") or "").strip()
        if snippet:
            lines.append(f"  证据：{snippet}")
        citations.append({"type": item["source_type"], "id": item["source_id"], "title": item["title"]})
    return "\n".join(lines), citations, results


def _checkout_root() -> Path:
    root = Path(settings.repo_checkout_dir)
    if not root.is_absolute():
        root = BACKEND_ROOT / root
    return root.resolve()


def _repo_checkout_path(repo: Repository) -> Path:
    local_path = getattr(repo, "local_path", None)
    if local_path:
        path = Path(os.path.expandvars(os.path.expanduser(str(local_path))))
        if not path.is_absolute():
            path = BACKEND_ROOT / path
        resolved = path.resolve()
        if resolved.exists():
            return resolved

    safe_name = repo.full_name.replace("/", "__").replace("\\", "__")
    checkout_root = _checkout_root()
    expected = (checkout_root / f"{repo.id}-{safe_name}").resolve()
    if expected.exists():
        return expected

    # 仓库记录可能被删除后重建，而本地 clone 仍保留旧 UUID。
    # 复用同名 checkout，避免工作区工具静默失去源码访问能力。
    candidates = [
        path
        for path in checkout_root.glob(f"*-{safe_name}")
        if path.is_dir() and path.resolve() != expected
    ]
    if candidates:
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0].resolve()
    return expected


def _workspace_root(repo: Repository | None) -> Path:
    if repo is None:
        return PROJECT_ROOT
    checkout_path = _repo_checkout_path(repo)
    if not checkout_path.exists():
        raise ValueError(
            f"{repo.full_name} 的仓库代码还没有克隆到本地。"
            "读取源码前，请先同步仓库或重新连接仓库。"
        )
    return checkout_path


def _workspace_relative(path: Path, workspace_root: Path) -> str:
    return path.relative_to(workspace_root).as_posix()


def _workspace_path(workspace_root: Path, raw_path: str | None = None) -> Path:
    normalized_path = str(raw_path or ".").strip()
    if normalized_path in {"", ".", "./", "/", "\\"}:
        normalized_path = "."
    elif not re.match(r"^[a-zA-Z]:", normalized_path):
        normalized_path = normalized_path.lstrip("/\\") or "."
    candidate = (workspace_root / normalized_path).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("路径位于项目工作区之外。") from exc
    if any(part in WORKSPACE_SKIP_DIRS for part in candidate.relative_to(workspace_root).parts):
        raise ValueError("路径位于被跳过的工作区目录中。")
    if candidate.name in WORKSPACE_SECRET_FILENAMES:
        raise ValueError("不会向对话 Agent 暴露敏感配置文件。")
    return candidate


def _is_workspace_text(path: Path) -> bool:
    if path.name in WORKSPACE_SECRET_FILENAMES or path.suffix.lower() in {".log", ".db", ".sqlite", ".sqlite3"}:
        return False
    return path.name in WORKSPACE_TEXT_FILENAMES or path.suffix.lower() in WORKSPACE_TEXT_EXTENSIONS or path.name.lower().endswith("dockerfile")


def _iter_workspace_files(root: Path, workspace_root: Path, max_files: int = 1000):
    if root.is_file():
        yield root
        return
    count = 0
    for path in root.rglob("*"):
        if count >= max_files:
            break
        if not path.is_file():
            continue
        relative_parts = path.relative_to(workspace_root).parts
        if any(part in WORKSPACE_SKIP_DIRS for part in relative_parts):
            continue
        if not _is_workspace_text(path):
            continue
        count += 1
        yield path


def _read_workspace_text(path: Path, max_bytes: int = 250_000) -> str:
    if not path.exists() or not path.is_file():
        raise ValueError("未找到工作区文件。")
    if not _is_workspace_text(path):
        raise ValueError("只能读取非敏感的文本工作区文件。")
    data = path.read_bytes()
    if len(data) > max_bytes or b"\x00" in data[:4096]:
        raise ValueError("文件过大，或看起来是二进制文件。")
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "latin-1"]:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法将工作区文件解码为文本。")


def _draft_for_action(action: str, message: str, repo: Repository, issues: list[Issue], prs: list[PullRequest], runs: list[WorkflowRun]) -> tuple[str, list[dict[str, str]]]:
    citations: list[dict[str, str]] = [{"type": "repository", "id": str(repo.id), "title": repo.full_name}]
    latest_issue = sorted(issues, key=_item_time, reverse=True)[0] if issues else None
    latest_pr = sorted(prs, key=_item_time, reverse=True)[0] if prs else None
    failed_runs = [run for run in runs if run.conclusion == "failure"]
    if action == "create_issue":
        return f"## Issue 草稿\n标题：{message.strip()[:120]}\n\n### 背景\n请补充影响范围、复现步骤和预期行为。\n\n### 验收标准\n- 预期行为清晰\n- 包含验证步骤\n", citations
    if action == "comment" and latest_pr:
        citations.append({"type": "pull_request", "id": str(latest_pr.id), "title": latest_pr.title})
        return f"## PR 评论草稿\n针对 PR #{latest_pr.number}：{latest_pr.title}\n\n合并前请确认测试覆盖、未解决的 Review 评论，以及是否存在高风险文件。\n", citations
    if action == "comment" and latest_issue:
        citations.append({"type": "issue", "id": str(latest_issue.id), "title": latest_issue.title})
        return f"## Issue 评论草稿\n针对 Issue #{latest_issue.number}：{latest_issue.title}\n\n请补充复现步骤、影响范围、预期行为，以及当前是否有临时绕过方案。\n", citations
    if action == "send_report":
        draft = (
            "## 报告发送草稿\n"
            f"仓库：{repo.full_name}\n\n"
            f"- 打开的 Issue：{len([issue for issue in issues if issue.state == 'open'])}\n"
            f"- 打开的 PR：{len([pr for pr in prs if pr.state == 'open'])}\n"
            f"- 失败 CI 运行：{len(failed_runs)}\n\n"
            "发送前请确认收件人、日期范围和敏感日志内容。\n"
        )
        return draft, citations
    return f"## 写操作草稿\n这可能会写入 GitHub 或外部渠道。MVP 模式只生成动作草稿。\n\n用户请求：{message.strip()}\n", citations


def _safe_confidence(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _skipped_workflow_result(task: WorkflowTask, summary: str) -> WorkflowTaskResult:
    return WorkflowTaskResult(
        task_id=task.id,
        agent_name=task.agent_name,
        task_type=task.task_type,
        status="skipped",
        summary=summary,
        confidence=0.0,
    )


def _agent_context(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key not in {"tools", "_skill_registry"}}


class WorkflowTargetResolutionError(ValueError):
    """Raised when an explicitly requested workflow target cannot be resolved safely."""


_WORKFLOW_TARGET_PATTERNS: dict[str, tuple[str, ...]] = {
    "issue": (
        r"(?<![A-Za-z0-9_])issue(?![A-Za-z0-9_])\s*(?:#|＃|no\.?|number|id)?\s*(\d+)",
        r"(?:工单|议题)\s*(?:#|＃|编号)?\s*(\d+)",
    ),
    "pull_request": (
        r"(?<![A-Za-z0-9_])(?:pr|pull[\s_-]*request)(?![A-Za-z0-9_])\s*(?:#|＃|no\.?|number|id)?\s*(\d+)",
        r"合并请求\s*(?:#|＃|编号)?\s*(\d+)",
    ),
    "workflow_run": (
        r"(?<![A-Za-z0-9_])(?:ci|workflow[\s_-]*run)(?![A-Za-z0-9_])\s*(?:#|＃|no\.?|number|id)?\s*(\d+)",
        r"(?<![A-Za-z0-9_])run(?![A-Za-z0-9_])\s*(?:#|＃|id)\s*(\d+)",
        r"(?:CI\s*运行|工作流运行|流水线)\s*(?:#|＃|编号|ID)?\s*(\d+)",
    ),
}

_WORKFLOW_TARGET_LABELS = {
    "issue": "Issue",
    "pull_request": "PR",
    "workflow_run": "CI/Workflow Run",
}


def _requested_workflow_target_number(message: str, target_type: str) -> int | None:
    numbers = {
        int(match.group(1))
        for pattern in _WORKFLOW_TARGET_PATTERNS[target_type]
        for match in re.finditer(pattern, message, flags=re.IGNORECASE)
    }
    if len(numbers) > 1:
        references = "、".join(f"#{number}" for number in sorted(numbers))
        raise WorkflowTargetResolutionError(
            f"一次工作流只能为一个 {_WORKFLOW_TARGET_LABELS[target_type]} 建立明确 claim；检测到多个目标：{references}。"
        )
    return next(iter(numbers), None)


def _target_by_number(
    items: list[Any],
    number: int | None,
    *,
    target_type: str,
    number_attribute: str,
) -> Any | None:
    if number is None:
        return None
    target = next((item for item in items if getattr(item, number_attribute, None) == number), None)
    if target is None:
        raise WorkflowTargetResolutionError(
            f"当前仓库没有同步 {_WORKFLOW_TARGET_LABELS[target_type]} #{number}，已停止生成计划；不会回退到最新对象。"
        )
    return target


def _build_engineering_workflow_runtime(
    repo: Repository,
    db: Session,
    message: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    issues, prs, runs = _repo_payload(repo.id, db)
    failed_runs = [run for run in runs if run.conclusion == "failure"]
    requested_issue_number = _requested_workflow_target_number(message, "issue")
    requested_pr_number = _requested_workflow_target_number(message, "pull_request")
    requested_run_number = _requested_workflow_target_number(message, "workflow_run")
    selected_issue = _target_by_number(
        issues,
        requested_issue_number,
        target_type="issue",
        number_attribute="number",
    )
    selected_pr = _target_by_number(
        prs,
        requested_pr_number,
        target_type="pull_request",
        number_attribute="number",
    )
    selected_run = _target_by_number(
        runs,
        requested_run_number,
        target_type="workflow_run",
        number_attribute="github_run_id",
    )
    if selected_pr is None:
        selected_pr = sorted(prs, key=_item_time, reverse=True)[0] if prs else None
    if selected_issue is None:
        if re.search(r"(?:最新\s*(?:的\s*)?(?:Issue|工单|议题)|latest\s+issue)", message, re.I):
            selected_issue = sorted(issues, key=_item_time, reverse=True)[0] if issues else None
        elif selected_pr:
            linked = {int(match) for match in re.findall(r"(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|关联|需求)\s+#(\d+)", selected_pr.body or "", re.I)}
            if len(linked) == 1:
                selected_issue = next((issue for issue in issues if issue.number in linked), None)
    if selected_run is None:
        from app.services.review_snapshot import ci_relationship
        matching = [run for run in failed_runs if ci_relationship(selected_pr, run) in {"pr_head", "synthetic_merge"}]
        selected_run = sorted(matching, key=_item_time, reverse=True)[0] if matching else None

    safe_context = _agent_context(context)
    workflow_context = {
        **safe_context,
        "repo_id": str(repo.id),
        "repo_full_name": repo.full_name,
        "has_repo": True,
        "has_issue": selected_issue is not None,
        "issue_id": str(selected_issue.id) if selected_issue else None,
        "issue_number": selected_issue.number if selected_issue else None,
        "issue_explicit": requested_issue_number is not None,
        "has_pr": selected_pr is not None,
        "pr_id": str(selected_pr.id) if selected_pr else None,
        "pr_number": selected_pr.number if selected_pr else None,
        "pr_explicit": requested_pr_number is not None,
        "has_ci_run": selected_run is not None,
        "has_failed_ci": selected_run is not None and selected_run.conclusion == "failure",
        "workflow_run_id": str(selected_run.id) if selected_run else None,
        "workflow_run_number": selected_run.github_run_id if selected_run else None,
        "workflow_run_explicit": requested_run_number is not None,
        "requires_safety": bool(_risky_action(message)),
        "team_members": safe_context.get("team_members") or [],
    }
    return workflow_context


def _workflow_report_status(report: dict[str, Any]) -> str:
    metrics = report.get("metrics") or {}
    if int(metrics.get("error_count") or 0):
        return "error"
    findings = (report.get("observation") or {}).get("findings") or []
    if int(metrics.get("model_fallback_count") or 0) or int(metrics.get("skipped_count") or 0) or any(item.get("severity") in {"warning", "blocker"} for item in findings):
        return "partial"
    return "success"


def _persist_engineering_workflow(
    db: Session,
    repo: Repository | None,
    conversation: Conversation | None,
    report: dict[str, Any],
) -> uuid.UUID:
    spec = report.get("spec") or {}
    task_by_id = {
        str(task.get("id")): task
        for task in spec.get("tasks", [])
        if isinstance(task, dict) and task.get("id")
    }
    workflow = AgentWorkflowRun(
        repo_id=repo.id if repo else None,
        conversation_id=conversation.id if conversation else None,
        goal=str(spec.get("goal") or ""),
        spec_json=spec,
        observation_json=report.get("observation") or {},
        final_answer=str(report.get("final_answer") or ""),
        metrics=report.get("metrics") or {},
        status=_workflow_report_status(report),
        completed_at=datetime.now(timezone.utc),
    )
    db.add(workflow)
    db.flush()
    for result in report.get("task_results") or []:
        if not isinstance(result, dict):
            continue
        task_id = str(result.get("task_id") or "")
        task = task_by_id.get(task_id, {})
        db.add(
            AgentTaskRun(
                workflow_id=workflow.id,
                task_id=task_id,
                agent_name=str(result.get("agent_name") or ""),
                task_type=str(result.get("task_type") or ""),
                claim_json=task.get("claim") if isinstance(task.get("claim"), dict) else {},
                input_json={"objective": task.get("objective"), "dependencies": task.get("dependencies", [])},
                result_json=result,
                status=str(result.get("status") or "unknown"),
                error=result.get("error"),
            )
        )
    return workflow.id


def _build_conversation_tools(repo: Repository | None, conversation: Conversation | None, db: Session) -> dict[str, Any]:
    mcp_client = StdioMcpClient()

    def agent_context(context: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in context.items() if key not in {"tools", "_skill_registry"}}

    async def call_memory_mcp(tool_name: str, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(input_data)
        if repo:
            arguments["repo_id"] = str(repo.id)
        if conversation:
            arguments["conversation_id"] = str(conversation.id)
        if tool_name == "devcooper_search_evidence":
            arguments.setdefault("query", input_data.get("message") or "")
        if context.get("evaluation_mode"):
            # The stdio server opens the production database. Run its same
            # handlers locally so evaluation writes stay in the trial database.
            memory = MemoryTools(db, repo, conversation)
            handler = {
                "devcooper_search_evidence": memory.search_evidence,
                "devcooper_get_thread_context": memory.get_thread_context,
                "devcooper_read_session_events": memory.read_session_events,
            }[tool_name]
            local = await handler(arguments, context)
            label = repo.full_name if repo else "全局"
            title = conversation.title if conversation else "全局"
            result = {"content": [{"type": "text", "text": f"仓库：{label}\n会话：{title}\n\n{local.get('answer') or ''}"}]}
        else:
            result = await mcp_client.call_tool(tool_name, arguments)
        text_parts = [
            str(item.get("text") or "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return {
            "route": tool_name.replace("devcooper_", ""),
            "tool_calls": [{"tool_name": tool_name, "arguments": arguments, "status": "success"}],
            "answer": "\n".join(text_parts).strip(),
            "citations": [],
        }

    async def search_evidence_mcp(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return await call_memory_mcp("devcooper_search_evidence", input_data, context)

    async def get_thread_context_mcp(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return await call_memory_mcp("devcooper_get_thread_context", input_data, context)

    async def read_session_events_mcp(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return await call_memory_mcp("devcooper_read_session_events", input_data, context)

    async def list_workspace_files(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        workspace_root = _workspace_root(repo)
        root = _workspace_path(workspace_root, str(input_data.get("path") or "."))
        limit = min(max(int(input_data.get("limit") or 80), 1), 200)
        raw_pattern = str(input_data.get("pattern") or "").strip().lower()
        rows: list[str] = []
        for path in _iter_workspace_files(root, workspace_root):
            relative = _workspace_relative(path, workspace_root)
            if raw_pattern and raw_pattern not in relative.lower():
                continue
            rows.append(relative)
            if len(rows) >= limit:
                break
        answer = "\n".join(rows) if rows else "没有找到匹配的工作区文件。"
        return {
            "route": "workspace_list_files",
            "tool_calls": [{"tool_name": "workspace.list_files", "arguments": {"path": str(input_data.get("path") or "."), "pattern": raw_pattern, "limit": limit}, "status": "success"}],
            "answer": answer,
            "citations": [{"type": "workspace", "id": str(workspace_root), "title": repo.full_name if repo else "项目工作区"}],
        }

    async def read_workspace_file(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(input_data.get("path") or "").strip()
        if not raw_path:
            raise ValueError("path 为必填项")
        workspace_root = _workspace_root(repo)
        path = _workspace_path(workspace_root, raw_path)
        text = _read_workspace_text(path)
        lines = text.splitlines()
        start_line = max(int(input_data.get("start_line") or 1), 1)
        line_count = min(max(int(input_data.get("line_count") or 160), 1), 400)
        end_line = min(start_line + line_count - 1, len(lines))
        numbered = [f"{line_number}: {lines[line_number - 1]}" for line_number in range(start_line, end_line + 1)]
        relative = _workspace_relative(path, workspace_root)
        return {
            "route": "workspace_read_file",
            "tool_calls": [{"tool_name": "workspace.read_file", "arguments": {"path": relative, "start_line": start_line, "line_count": line_count}, "status": "success"}],
            "answer": f"{relative}:{start_line}-{end_line}\n" + "\n".join(numbered),
            "citations": [{"type": "workspace_file", "id": relative, "title": relative}],
        }

    async def search_workspace_code(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        query = str(input_data.get("query") or input_data.get("message") or "").strip()
        if not query_terms(query):
            raise ValueError("query 为必填项")
        workspace_root = _workspace_root(repo)
        relative_path = str(input_data.get("path") or ".")
        _workspace_path(workspace_root, relative_path)
        limit = min(max(int(input_data.get("limit") or 12), 1), 50)
        matches = search_checkout_code(workspace_root, query, limit=limit, relative_path=relative_path)
        rows = [item["snippet"] for item in matches]
        citations = [
            {"type": "workspace_file", "id": item["source_id"], "title": item["title"]}
            for item in matches
        ]
        return {
            "route": "workspace_search_code",
            "tool_calls": [{"tool_name": "workspace.search_code", "arguments": {"query": query, "path": relative_path, "limit": limit}, "status": "success"}],
            "answer": "\n".join(rows) if rows else "没有找到匹配的工作区代码。",
            "citations": citations,
        }

    async def repo_health(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return {"route": "unknown", "tool_calls": [], "answer": "当前还没有可用仓库，请先连接仓库。", "citations": []}
        issues, prs, runs = _repo_payload(repo.id, db)
        snapshot = repository_health_snapshot(repo, issues, prs, runs)
        return {
            "route": "repo_health",
            "tool_calls": [{"tool_name": "repo_health.generate_summary", "arguments": {"repo_id": str(repo.id)}, "status": snapshot["status"]}],
            "data_status": snapshot["data_status"],
            "answer": _repo_health_answer(repo, issues, prs, runs),
            "citations": [{"type": "repository", "id": str(repo.id), "title": repo.full_name}],
        }

    async def semantic_search(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)
        original_message = str(input_data.get("message") or "").strip()
        query = str(input_data.get("query") or original_message).strip()
        if not query:
            raise ValueError("query 为必填项")
        answer, citations, results = _semantic_search(repo, query, context, db)
        record_recall_event(
            db,
            repo_id=repo.id,
            conversation_id=conversation.id if conversation else None,
            tool_name="rag.search_similar_documents",
            query=query,
            results=results,
            citations=citations,
            scope="conversation" if conversation else "project",
            mode="hybrid",
            metadata={
                "source_type": context.get("source_type"),
                "limit": context.get("limit", 5),
                "original_message": original_message,
                "query_rewritten": bool(original_message and query != original_message),
            },
        )
        return {
            "route": "semantic_search",
            "tool_calls": [
                {
                    "tool_name": "rag.search_similar_documents",
                    "arguments": {"repo_id": str(repo.id), "query": query},
                    "status": "success",
                }
            ],
            "answer": answer,
            "citations": citations,
            "retrieved_contexts": [
                str(item.get("snippet") or "")
                for item in results
                if str(item.get("snippet") or "").strip()
            ],
            "retrieval_results": [
                {
                    "id": str(item.get("id") or ""),
                    "source_id": str(item.get("source_id") or ""),
                    "source_type": str(item.get("source_type") or ""),
                    "title": str(item.get("title") or ""),
                    "score": item.get("score"),
                    "snippet": str(item.get("snippet") or ""),
                    "retrieval": item.get("retrieval") or {},
                }
                for item in results
            ],
        }

    async def analyze_issue(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)
        return await IssueAgent().run(input_data, {"db": db, "repo_id": repo.id, "conversation": conversation})

    async def analyze_pr(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)
        return await PRReviewAgent().run(input_data, {"db": db, "repo_id": repo.id, "conversation": conversation})

    async def analyze_ci(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)
        return await CIDebugAgent().run(input_data, {"db": db, "repo_id": repo.id, "conversation": conversation})

    async def generate_report(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)
        issues, prs, runs = _repo_payload(repo.id, db)
        start_at, end_at = _date_window_from_context(context)
        scoped_issues = [item for item in issues if any(_within(value, start_at, end_at) for value in [item.created_at, item.updated_at, item.closed_at])]
        scoped_prs = [item for item in prs if any(_within(value, start_at, end_at) for value in [item.created_at, item.updated_at, item.merged_at])]
        scoped_runs = [item for item in runs if any(_within(value, start_at, end_at) for value in [item.created_at, item.updated_at])]
        result = await ReportAgent().run(
            {
                "issues": [{"number": item.number, "title": item.title, "state": item.state, "closed_at": item.closed_at} for item in scoped_issues],
                "pull_requests": [{"number": item.number, "title": item.title, "state": item.state, "merged_at": item.merged_at} for item in scoped_prs],
                "workflow_runs": [{"name": item.name, "conclusion": item.conclusion} for item in scoped_runs],
                "metrics": {
                    "issues": len(scoped_issues),
                    "pull_requests": len(scoped_prs),
                    "merged_prs": len([item for item in scoped_prs if item.merged_at]),
                    "open_prs": len([item for item in scoped_prs if item.state == "open"]),
                    "failed_ci": len([item for item in scoped_runs if item.conclusion == "failure"]),
                },
            },
            {"repo_id": str(repo.id), "repo_name": repo.full_name, "start_date": start_at.date().isoformat(), "end_date": end_at.date().isoformat()},
        )
        add_document(
            db,
            repo.id,
            "weekly_report",
            None,
            f"研发周报 {start_at.date().isoformat()} - {end_at.date().isoformat()}",
            result["report_markdown"],
            {
                "display_title": f"研发周报 {start_at.date().isoformat()} - {end_at.date().isoformat()}",
                "start_date": start_at.date().isoformat(),
                "end_date": end_at.date().isoformat(),
                "repo_name": repo.full_name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "chat",
            },
            commit=False,
        )
        return {
            "route": "weekly_report",
            "tool_calls": [{"tool_name": "report_agent.generate_weekly_report", "arguments": {"repo_id": str(repo.id)}, "status": "success"}],
            "answer": result["report_markdown"],
            "citations": [{"type": "repository", "id": str(repo.id), "title": repo.full_name}],
        }

    async def check_safety(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)
        issues, prs, runs = _repo_payload(repo.id, db)
        action = _risky_action(input_data["message"]) or "write_back"
        draft, citations = _draft_for_action(action, input_data["message"], repo, issues, prs, runs)
        safety = await SafetyAgent().run({"action": action, "draft": draft}, agent_context(context))
        latest_issue = sorted(issues, key=_item_time, reverse=True)[0] if issues else None
        latest_pr = sorted(prs, key=_item_time, reverse=True)[0] if prs else None
        target_type = "repository"
        target_id = None
        metadata: dict[str, Any] = {"source": "chat", "message": input_data["message"]}
        if action == "comment" and latest_pr:
            target_type = "pull_request"
            target_id = latest_pr.id
            metadata["pr_number"] = latest_pr.number
        elif action in {"comment", "create_issue", "close_issue"} and latest_issue:
            target_type = "issue"
            target_id = latest_issue.id
            metadata["issue_number"] = latest_issue.number
        draft_type = {
            "comment": "pr_comment" if target_type == "pull_request" else "issue_comment",
            "create_issue": "create_issue",
            "send_report": "send_report",
            "write_back": "comment",
        }.get(action, action)
        user = ensure_demo_user(db)
        action_draft = ActionDraft(
            repo_id=repo.id,
            draft_type=draft_type,
            target_type=target_type,
            target_id=target_id,
            title=f"{draft_type} 草稿",
            content=str(safety.get("draft") or draft),
            risk_level=str(safety.get("risk_level") or "medium"),
            created_by=user.id,
            meta=metadata,
        )
        db.add(action_draft)
        db.flush()
        write_audit_log(
            db,
            user=user,
            repo_id=repo.id,
            action="action_draft.create_from_chat",
            target_type=target_type,
            target_id=str(target_id) if target_id else None,
            request_json={"action": action, "draft_type": draft_type},
            result_json={"draft_id": str(action_draft.id)},
        )
        return {
            "route": "safety_draft",
            "tool_calls": [{"tool_name": "safety_agent.classify_action_risk", "arguments": {"action": action, "draft_id": str(action_draft.id)}, "status": "success"}],
            "answer": f"这个请求可能写入 GitHub 或发送到外部渠道，已生成待确认安全草稿。\n\n草稿 ID：{action_draft.id}\n\n{str(safety.get('draft') or draft)}",
            "citations": citations,
        }

    async def run_engineering_workflow(input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if repo is None:
            return await repo_health(input_data, context)

        message = str(input_data.get("message") or input_data.get("query") or "").strip()
        return WorkflowOrchestrator.submit(db, repo, conversation, message or context.get("message") or "Run an engineering workflow.", context)

    return {
        "workspace.list_files": list_workspace_files,
        "workspace.read_file": read_workspace_file,
        "workspace.search_code": search_workspace_code,
        "repo_health.generate_summary": repo_health,
        "rag.search_similar_documents": semantic_search,
        "issue_agent.analyze_issue": analyze_issue,
        "pr_review_agent.analyze_pr": analyze_pr,
        "ci_debug_agent.analyze_workflow_run": analyze_ci,
        "report_agent.generate_weekly_report": generate_report,
        "safety_agent.classify_action_risk": check_safety,
        "workflow.run_engineering_review": run_engineering_workflow,
        "devcooper_search_evidence": search_evidence_mcp,
        "devcooper_get_thread_context": get_thread_context_mcp,
        "devcooper_read_session_events": read_session_events_mcp,
    }


def _skill_runtime_context() -> dict[str, Any]:
    registry = get_skill_registry()
    return {
        "_skill_registry": registry,
        "registered_skills": registry.prompt_context(),
        "skill_trace_by_tool": registry.trace_metadata_by_tool(),
        "skill_description_by_tool": registry.tool_description_suffixes(),
    }


def _engineering_workflow_response(repo: Repository, workflow_run_id: uuid.UUID, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": "engineering_workflow",
        "tool_calls": [
            {
                "tool_name": "workflow.run_engineering_review",
                "arguments": {"repo_id": str(repo.id), "workflow_run_id": str(workflow_run_id)},
                "status": _workflow_report_status(report),
            },
            *report.get("tool_calls", []),
        ],
        "answer": report.get("final_answer") or "",
        "citations": report.get("citations") or [],
        "agent_events": report.get("agent_events") or [],
    }


def _workflow_plan_answer(spec: WorkflowSpec) -> str:
    lines = [
        "已创建执行计划，确认后我会按这个 WorkflowSpec 执行。",
        "",
        f"目标：{spec.goal}",
        f"规划来源：{execution_label(spec.planning)}",
        "",
        "任务：",
    ]
    for index, task in enumerate(spec.tasks, start=1):
        deps = f"；依赖：{', '.join(task.dependencies)}" if task.dependencies else ""
        critical = "；关键任务" if task.critical else ""
        lines.append(f"{index}. {task.agent_name} / {task.task_type}: {task.objective}{deps}{critical}")
    if spec.acceptance_criteria:
        lines.extend(["", "验收标准："])
        lines.extend(f"- {item}" for item in spec.acceptance_criteria)
    return "\n".join(lines)


async def _conversation_plan(payload: ChatRequest, db: Session) -> dict[str, Any]:
    repo = _repo_or_404(db, payload.repo_id)
    conversation = ensure_conversation(db, repo.id, payload.conversation_id)
    db.commit()
    context = {
        **_client_context(payload.context),
        **_skill_runtime_context(),
        "repo_id": str(repo.id),
        "conversation_id": str(conversation.id),
    }
    workflow_context = _build_engineering_workflow_runtime(repo, db, payload.message, context)
    import asyncio
    from app.services.agents.investigation import structured_decision
    from app.services.agents.workflow_models import PLAN_PROMPT, PlanProposal
    from app.schemas.workflow import WorkflowModelExecution
    planner = PlannerAgent()
    catalog = planner._task_catalog(workflow_context)
    catalog.pop("rag_search", None)
    db.commit()
    proposal = await asyncio.wait_for(structured_decision(PLAN_PROMPT, {"goal": payload.message,
        "phase": "initial_plan", "task_catalog": planner._catalog_payload(catalog)}, PlanProposal), 30)
    for proposed in proposal.tasks:
        if proposed.task_type in {"issue_analysis", "pr_review", "ci_debug"}:
            proposed.dependencies = []
    spec = planner._apply_proposal(proposal, WorkflowSpec(goal=payload.message, trigger_message=payload.message), catalog)
    for task in spec.tasks:
        if task.task_type in {"issue_analysis", "pr_review", "ci_debug"}:
            task.dependencies = []
    spec.planning = spec.initial_planning = WorkflowModelExecution(mode="llm", model=settings.llm_model)
    spec_json = spec.model_dump(mode="json")
    return {
        "route": "engineering_plan",
        "conversation_id": str(conversation.id),
        "answer": _workflow_plan_answer(spec),
        "plan": spec_json,
        "tool_calls": [
            {
                "tool_name": "planner_agent.create_workflow_spec",
                "arguments": {"repo_id": str(repo.id), "workflow_id": spec.workflow_id},
                "status": "success",
            }
        ],
        "agent_events": [
            {
                "type": "workflow_spec",
                "workflow_id": spec.workflow_id,
                "goal": spec.goal,
                "planning": spec.planning.model_dump(mode="json") if spec.planning else None,
                "tasks": [
                    {
                        "id": task.id,
                        "agent_name": task.agent_name,
                        "task_type": task.task_type,
                        "dependencies": task.dependencies,
                        "claim": task.claim.model_dump(mode="json"),
                    }
                    for task in spec.tasks
                ],
            }
        ],
    }


def _workflow_execution_trace_events(report: dict[str, Any], workflow_tool_id: str) -> list[dict[str, Any]]:
    events = [
        {
            "event": "tool_result",
            "data": {
                "tool_use_id": workflow_tool_id,
                "tool_name": "workflow.run_engineering_review",
                "status": "success",
                "route": "engineering_workflow",
                "content": {
                    "answer_preview": "Confirmed plan executed.",
                    "citations_count": len(report.get("citations") or []),
                },
            },
        }
    ]
    for item in report.get("agent_events") or []:
        if not isinstance(item, dict):
            continue
        event_type = item.get("type")
        if event_type == "workflow_task_result":
            events.append(
                {
                    "event": "tool_result",
                    "data": {
                        "tool_use_id": f"{item.get('workflow_id')}_{item.get('task_id')}",
                        "tool_name": f"workflow.{item.get('task_type')}",
                        "status": item.get("status") or "success",
                        "route": "engineering_workflow",
                        "content": {
                            "answer_preview": item.get("summary") or "",
                            "citations_count": item.get("evidence_count") or 0,
                        },
                    },
                }
            )
        elif event_type == "workflow_replan":
            events.append(
                {
                    "event": "tool_result",
                    "data": {
                        "tool_use_id": f"{item.get('workflow_id')}_replan_{item.get('iteration')}",
                        "tool_name": "planner_agent.replan_workflow_spec",
                        "status": "success",
                        "route": "engineering_workflow",
                        "content": {
                            "answer_preview": (
                                f"Replan #{item.get('iteration')}: {item.get('reason')}; "
                                f"added tasks={', '.join(str(task) for task in item.get('added_tasks') or []) or 'none'}"
                            ),
                            "citations_count": 0,
                        },
                    },
                }
            )
        elif event_type == "workflow_observer_result":
            events.append(
                {
                    "event": "tool_result",
                    "data": {
                        "tool_use_id": f"{item.get('workflow_id')}_observer",
                        "tool_name": "workflow.observer",
                        "status": "success",
                        "route": "engineering_workflow",
                        "content": {
                            "answer_preview": f"Observer confidence={item.get('overall_confidence')}, findings={len(item.get('findings') or [])}",
                            "citations_count": 0,
                        },
                    },
                }
            )
    return events


def _prepare_chat_evidence(db: Session, repo: Repository, conversation: Conversation, message: str):
    try:
        user_message = MessageStore(db).append(repo_id=repo.id, conversation_id=conversation.id, role="user", content=message)
        EvidenceStore(db).rebuild_repo(repo, conversation)
        db.commit()
        assembled = ContextAssembler(db).assemble(repo, conversation, message)
        db.commit()
        return user_message, assembled
    except BaseException:
        db.rollback()
        raise


async def _conversation_plan_execute_sse(payload: ChatPlanExecuteRequest, db: Session):
    repo = _repo_or_404(db, payload.repo_id)

    conversation = ensure_conversation(db, repo.id, payload.conversation_id)
    if conversation:
        db.commit()
    compaction_stats = await ProgressiveContextManager(db).ensure_headroom(repo, conversation, payload.message)
    if compaction_stats.get("attempted"):
        db.commit()
    message_store = MessageStore(db)
    user_message, assembled = await run_blocking(_prepare_chat_evidence, db, repo, conversation, payload.message)
    context = {
        **_client_context(payload.context),
        **_skill_runtime_context(),
        "repo_id": str(repo.id),
        "conversation_id": str(conversation.id),
        "server_context": assembled.system_context,
        "server_messages": assembled.history_messages,
        "evidence": assembled.evidence,
        "compression_stats": assembled.compression_stats,
        "progressive_compaction": compaction_stats,
    }
    workflow_tool_id = f"confirmed_{payload.workflow_spec.get('workflow_id') or uuid.uuid4().hex[:8]}"
    try:
        spec = WorkflowSpec.model_validate(payload.workflow_spec)
        yield encode_sse(
            {
                "event": "tool_use",
                "data": {
                    "id": workflow_tool_id,
                    "tool_name": "workflow.run_engineering_review",
                    "arguments": {"repo_id": str(repo.id), "workflow_id": spec.workflow_id, "confirmed_plan": True},
                },
            }
        )
        result = WorkflowOrchestrator.submit(db, repo, conversation, payload.message, context, spec)
        yield encode_sse({"event": "agent_event", "data": result["agent_events"][0]})
        run_id = uuid.uuid4()
        result["run_id"] = str(run_id)
        result["compression_stats"] = assembled.compression_stats
        result["progressive_compaction"] = compaction_stats
        for index in range(0, len(result["answer"]), 48):
            yield encode_sse({"event": "delta", "data": {"content": result["answer"][index : index + 48]}})

        assistant_message = _append_assistant_response(message_store, db,
            repo_id=repo.id,
            conversation_id=conversation.id,
            role="assistant",
            content=result["answer"],
            route=result["route"],
            tool_calls=result["tool_calls"],
            meta={
                "run_id": str(run_id),
                "agent_events": result.get("agent_events", []),
                "model_usage": result.get("model_usage", []),
                "context_diagnostics": result.get("context_diagnostics", {}),
                "compression_stats": assembled.compression_stats,
                "progressive_compaction": compaction_stats,
            },
        )
        result["message_id"] = str(assistant_message.id)
        result["answer"] = assistant_message.content
        db.add(AgentRun(id=run_id, repo_id=repo.id, conversation_id=conversation.id, user_message=payload.message, route=result["route"], tool_calls=result["tool_calls"], final_answer=result["answer"], status=assistant_message.meta.get("workflow_status", "success")))
        touch_conversation(db, conversation, user_message=payload.message, increment=2)
        await _seal_chat_turn(db,
            repo=repo,
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            route=result["route"],
            tool_calls=result["tool_calls"],
            agent_events=result.get("agent_events", []),
            model_usage=result.get("model_usage", []),
            context_diagnostics=result.get("context_diagnostics", {}),
        )
        db.commit()
        yield encode_sse({"event": "final", "data": result})
    except Exception as exc:
        db.rollback()
        run_id = uuid.uuid4()
        answer = f"执行计划失败：{exc}"
        assistant_message = _append_assistant_response(message_store, db,
            repo_id=repo.id,
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            route="error",
            tool_calls=[],
            meta={"run_id": str(run_id), "error": str(exc)},
        )
        db.add(AgentRun(id=run_id, repo_id=repo.id, conversation_id=conversation.id, user_message=payload.message, route="error", tool_calls=[], final_answer=answer, status="error"))
        touch_conversation(db, conversation, user_message=payload.message, increment=2)
        await _seal_chat_turn(db,
            repo=repo,
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            route="error",
            tool_calls=[],
            agent_events=[],
        )
        db.commit()
        yield encode_sse(
            {
                "event": "final",
                "data": {
                    "run_id": str(run_id),
                    "message_id": str(assistant_message.id),
                    "route": "error",
                    "tool_calls": [],
                    "answer": answer,
                    "citations": [],
                    "agent_events": [],
                },
            }
        )


async def _conversation_response(payload: ChatRequest, db: Session) -> dict[str, Any]:
    repo = _repo_or_404(db, payload.repo_id)
    conversation = ensure_conversation(db, repo.id, payload.conversation_id)
    db.commit()
    compaction_stats = await ProgressiveContextManager(db).ensure_headroom(repo, conversation, payload.message)
    if compaction_stats.get("attempted"):
        db.commit()
    message_store = MessageStore(db)
    user_message, assembled = await run_blocking(_prepare_chat_evidence, db, repo, conversation, payload.message)
    context = {
        **_client_context(payload.context),
        **_skill_runtime_context(),
        "repo_id": str(repo.id),
        "conversation_id": str(conversation.id),
        "server_context": assembled.system_context,
        "server_messages": assembled.history_messages,
        "evidence": assembled.evidence,
        "compression_stats": assembled.compression_stats,
        "progressive_compaction": compaction_stats,
    }
    context["tools"] = _build_conversation_tools(repo, conversation, db)
    result = await ChatAgent().run({"message": payload.message}, context)
    run_id = uuid.uuid4()
    result["run_id"] = str(run_id)
    result["compression_stats"] = assembled.compression_stats
    result["progressive_compaction"] = compaction_stats
    assistant_message: ChatMessage | None = None
    assistant_message = _append_assistant_response(message_store, db,
        repo_id=repo.id,
        conversation_id=conversation.id,
        role="assistant",
        content=result["answer"],
        route=result["route"],
        tool_calls=result["tool_calls"],
        meta={
            "run_id": str(run_id),
            "agent_events": result.get("agent_events", []),
            "model_usage": result.get("model_usage", []),
            "context_diagnostics": result.get("context_diagnostics", {}),
            "compression_stats": assembled.compression_stats,
            "progressive_compaction": compaction_stats,
        },
    )
    result["message_id"] = str(assistant_message.id)
    result["answer"] = assistant_message.content
    db.add(AgentRun(id=run_id, repo_id=repo.id, conversation_id=conversation.id, user_message=payload.message, route=result["route"], tool_calls=result["tool_calls"], final_answer=result["answer"], status=assistant_message.meta.get("workflow_status", "success")))
    touch_conversation(db, conversation, user_message=payload.message, increment=2)
    await _seal_chat_turn(db,
        repo=repo,
        conversation=conversation,
        user_message=user_message,
        assistant_message=assistant_message,
        route=result["route"],
        tool_calls=result["tool_calls"],
        agent_events=result.get("agent_events", []),
        model_usage=result.get("model_usage", []),
        context_diagnostics=result.get("context_diagnostics", {}),
    )
    db.commit()
    return result


async def _conversation_sse(payload: ChatRequest, db: Session):
    repo = _repo_or_404(db, payload.repo_id)
    conversation = ensure_conversation(db, repo.id, payload.conversation_id)
    db.commit()
    compaction_stats = await ProgressiveContextManager(db).ensure_headroom(repo, conversation, payload.message)
    if compaction_stats.get("attempted"):
        db.commit()
    message_store = MessageStore(db)
    user_message, assembled = await run_blocking(_prepare_chat_evidence, db, repo, conversation, payload.message)
    context = {
        **_client_context(payload.context),
        **_skill_runtime_context(),
        "repo_id": str(repo.id),
        "conversation_id": str(conversation.id),
        "server_context": assembled.system_context,
        "server_messages": assembled.history_messages,
        "evidence": assembled.evidence,
        "compression_stats": assembled.compression_stats,
        "progressive_compaction": compaction_stats,
    }
    context["tools"] = _build_conversation_tools(repo, conversation, db)
    try:
        async for event in ChatAgent().run_stream({"message": payload.message}, context):
            if event["event"] == "final":
                run_id = uuid.uuid4()
                event["data"]["run_id"] = str(run_id)
                event["data"]["compression_stats"] = assembled.compression_stats
                event["data"]["progressive_compaction"] = compaction_stats
                assistant_message = _append_assistant_response(message_store, db,
                    repo_id=repo.id,
                    conversation_id=conversation.id,
                    role="assistant",
                    content=event["data"]["answer"],
                    route=event["data"]["route"],
                    tool_calls=event["data"]["tool_calls"],
                    meta={
                        "run_id": str(run_id),
                        "agent_events": event["data"].get("agent_events", []),
                        "model_usage": event["data"].get("model_usage", []),
                        "context_diagnostics": event["data"].get("context_diagnostics", {}),
                        "compression_stats": assembled.compression_stats,
                        "progressive_compaction": compaction_stats,
                    },
                )
                event["data"]["message_id"] = str(assistant_message.id)
                event["data"]["answer"] = assistant_message.content
                db.add(AgentRun(id=run_id, repo_id=repo.id, conversation_id=conversation.id, user_message=payload.message, route=event["data"]["route"], tool_calls=event["data"]["tool_calls"], final_answer=event["data"]["answer"], status=assistant_message.meta.get("workflow_status", "success")))
                touch_conversation(db, conversation, user_message=payload.message, increment=2)
                await _seal_chat_turn(db,
                    repo=repo,
                    conversation=conversation,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    route=event["data"]["route"],
                    tool_calls=event["data"]["tool_calls"],
                    agent_events=event["data"].get("agent_events", []),
                    model_usage=event["data"].get("model_usage", []),
                    context_diagnostics=event["data"].get("context_diagnostics", {}),
                )
                db.commit()
            yield encode_sse(event)
    except Exception as exc:
        db.rollback()
        run_id = uuid.uuid4()
        answer = f"请求失败：{exc}"
        failed_event = {
            "event": "final",
            "data": {
                "run_id": str(run_id),
                "route": "error",
                "tool_calls": [],
                "answer": answer,
                "citations": [],
            },
        }
        assistant_message = _append_assistant_response(message_store, db,
            repo_id=repo.id,
            conversation_id=conversation.id,
            role="assistant",
            content=answer,
            route="error",
            tool_calls=[],
            meta={"run_id": str(run_id), "error": str(exc)},
        )
        failed_event["data"]["message_id"] = str(assistant_message.id)
        db.add(AgentRun(id=run_id, repo_id=repo.id, conversation_id=conversation.id, user_message=payload.message, route="error", tool_calls=[], final_answer=answer, status="error"))
        touch_conversation(db, conversation, user_message=payload.message, increment=2)
        await _seal_chat_turn(db,
            repo=repo,
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            route="error",
            tool_calls=[],
            agent_events=[],
        )
        db.commit()
        yield encode_sse(failed_event)


@router.post("/plan")
async def chat_plan(payload: ChatRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return await _conversation_plan(payload, db)
    except WorkflowTargetResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        from app.services.review_runs import call_error
        raise HTTPException(status_code=503, detail=call_error(exc, "planner")) from exc


@router.post("/plan/execute/stream")
async def chat_plan_execute_stream(payload: ChatPlanExecuteRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    return StreamingResponse(_conversation_plan_execute_sse(payload, db), media_type="text/event-stream")


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    return await _conversation_response(payload, db)


@router.post("/stream")
async def chat_stream(payload: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    return StreamingResponse(_conversation_sse(payload, db), media_type="text/event-stream")


@router.post("/recommendations", response_model=PromptRecommendationResponse)
async def prompt_recommendations(payload: PromptRecommendationRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    repo = _repo_or_404(db, payload.repo_id)
    issues, prs, runs = _repo_payload(repo.id, db)
    result = await RecommendationAgent().run(
        _recommendation_payload(repo, issues, prs, runs, payload.exclude, payload.limit),
        {"repo_id": str(repo.id)},
    )
    return {
        "agent": result["agent"],
        "suggestions": result["suggestions"],
    }
