import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.db.models import AnalysisResult, CodeRelation, CodeSymbol, Conversation, Document, PullRequest, Repository, WorkflowRun
from app.db.session import get_db
from app.schemas.ci import WorkflowRunResponse
from app.schemas.workflow import InvestigationSubmission
from app.services.chat_memory import ContextAssembler
from app.services.code_search import search_repository_code
from app.services.rag.indexing import sanitize_ci_log
from app.services.rag.retrieval import search_similar_documents
from app.services.worktree_manager import prepare_pr_worktree

router = APIRouter()

PROJECT_DOC_TYPES = {"knowledge_file", "memory_note", "project_doc"}


def _document_evidence(items: list[dict], default_source_type: str) -> list[dict]:
    evidence = []
    for item in items:
        snippet = str(item.get("snippet") or "").strip()
        title = str(item.get("title") or "").strip()
        if not snippet or not title:
            continue
        evidence.append(
            {
                "source_type": item.get("source_type") or default_source_type,
                "title": title,
                "snippet": snippet,
                "source_id": str(item.get("source_id") or item.get("id") or ""),
                "score": item.get("score"),
                "metadata": item.get("metadata") or {},
            }
        )
    return evidence


def _dedupe_evidence(items: list[dict], limit: int) -> list[dict]:
    output = []
    seen = set()
    for item in items:
        key = (item.get("source_type"), item.get("source_id"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output


def _fallback_project_documents(db: Session, repo_id: UUID) -> list[dict]:
    rows = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_type.in_(PROJECT_DOC_TYPES))
        .order_by(Document.created_at.desc())
        .limit(4)
        .all()
    )
    return [
        {
            "source_type": doc.source_type,
            "title": doc.title,
            "snippet": doc.content[:260],
            "source_id": str(doc.source_id),
            "score": None,
            "metadata": doc.meta or {},
        }
        for doc in rows
    ]


def _conversation_evidence(db: Session, run: WorkflowRun, conversation_id: UUID | None, query: str) -> list[dict]:
    if not conversation_id:
        return []
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repo_id != run.repo_id:
        return []
    repo = db.get(Repository, run.repo_id)
    assembled = ContextAssembler(db).assemble(repo, conversation, query, limit=8)
    evidence = _document_evidence(assembled.evidence, "conversation")
    if assembled.system_context.strip():
        evidence.append(
            {
                "source_type": "conversation",
                "title": f"会话上下文：{conversation.title}",
                "snippet": assembled.system_context[:320],
                "source_id": str(conversation.id),
                "score": None,
            }
        )
    return evidence[:5]


def _ci_plan() -> list[str]:
    return [
        "找出失败 job 和失败 step。",
        "提取首个关键错误块。",
        "判断失败类型：依赖、测试、构建、权限、环境变量或未知。",
        "搜索相关代码、测试文件和配置文件。",
        "关联最近 PR 或变更模块。",
        "输出根因、修复步骤和阻塞判断。",
    ]


def _workflow_payload(run: WorkflowRun) -> dict:
    return {
        "id": str(run.id),
        "name": run.name,
        "status": run.status,
        "conclusion": run.conclusion,
        "jobs": run.jobs or [],
        "logs_text": run.logs_text,
        "html_url": run.html_url,
    }


def _failed_jobs(run: WorkflowRun) -> list[dict]:
    return [
        {
            **job,
            "failed_steps": [step for step in job.get("steps", []) if step.get("conclusion") == "failure"],
        }
        for job in (run.jobs or [])
        if job.get("conclusion") == "failure"
    ]


def _first_error(logs: str | None) -> str | None:
    if not logs or not logs.strip():
        return None
    lines = [line.strip() for line in logs.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(token in lowered for token in ["error", "failed", "failure", "exception", "traceback", "assertionerror", "npm err", "fatal"]):
            start = max(0, index - 2)
            end = min(len(lines), index + 5)
            return "\n".join(lines[start:end])[:900]
    return "\n".join(lines[:6])[:900]


def _related_files_from_logs(logs: str | None) -> list[str]:
    if not logs:
        return []
    candidates = re.findall(r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|toml|ini|cfg|md)", logs)
    normalized = []
    for item in candidates:
        cleaned = item.strip("`'\"()[]{}:,;").replace("\\", "/")
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized[:8]


def _related_pr_for_run(db: Session, run: WorkflowRun) -> PullRequest | None:
    candidates = (
        db.query(PullRequest)
        .options(selectinload(PullRequest.files))
        .filter(PullRequest.repo_id == run.repo_id)
        .order_by(PullRequest.updated_at.desc(), PullRequest.created_at.desc())
        .limit(8)
        .all()
    )
    if not candidates:
        return None
    logs = f"{run.name}\n{run.logs_text or ''}".lower()
    for pr in candidates:
        if pr.head_branch and pr.head_branch.lower() in logs:
            return pr
    return candidates[0]


def _snapshot_summary(snapshot: dict) -> dict:
    return {
        "status": snapshot.get("status"),
        "message": snapshot.get("message"),
        "name": snapshot.get("snapshot_name") or "PR 分支快照",
        "branch": snapshot.get("branch"),
        "commit_sha": snapshot.get("commit_sha"),
        "pr_id": snapshot.get("pr_id"),
        "pr_number": snapshot.get("pr_number"),
        "indexed_symbols": snapshot.get("indexed_symbols"),
        "pull_request": snapshot.get("pull_request"),
    }


def _prepare_ci_pr_snapshot(db: Session, run: WorkflowRun, enabled: bool) -> dict:
    if not enabled:
        return {"status": "skipped", "message": "未请求 PR 分支快照。"}
    repo = db.get(Repository, run.repo_id)
    if repo is None:
        return {"status": "missing_repo", "message": "未找到仓库。"}
    pr = _related_pr_for_run(db, run)
    if pr is None:
        return {"status": "missing_pr", "message": "未找到可关联的 PR。"}
    snapshot = prepare_pr_worktree(db, repo, pr)
    snapshot["pull_request"] = {
        "id": str(pr.id),
        "number": pr.number,
        "title": pr.title,
        "head_branch": pr.head_branch,
        "state": pr.state,
    }
    return snapshot


def _ci_code_graph_impact(db: Session, pr_id: UUID, related_files: list[str]) -> dict:
    paths = list(dict.fromkeys(related_files))
    symbol_query = db.query(CodeSymbol).filter(CodeSymbol.pr_id == pr_id)
    if paths:
        symbol_query = symbol_query.filter(CodeSymbol.path.in_(paths))
    symbols = symbol_query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc()).limit(30).all()
    symbol_names = [symbol.name for symbol in symbols]
    relation_query = db.query(CodeRelation).filter(CodeRelation.pr_id == pr_id)
    relation_filters = []
    if paths:
        relation_filters.append(CodeRelation.path.in_(paths))
    if symbol_names:
        relation_filters.extend([CodeRelation.source_name.in_(symbol_names), CodeRelation.target_name.in_(symbol_names)])
    if relation_filters:
        relation_query = relation_query.filter(or_(*relation_filters))
    relations = relation_query.limit(60).all()
    return {
        "related_files": paths[:30],
        "symbols": [
            {
                "name": symbol.name,
                "kind": symbol.kind,
                "path": symbol.path,
                "start_line": symbol.start_line,
                "branch": symbol.branch,
                "commit_sha": symbol.commit_sha,
            }
            for symbol in symbols
        ],
        "relations": [
            {
                "source": relation.source_name,
                "target": relation.target_name,
                "type": relation.relation_type,
                "path": relation.path,
                "line": (relation.meta or {}).get("line"),
            }
            for relation in relations
        ],
    }


def _augment_ci_context_with_snapshot(db: Session, run: WorkflowRun, query: str, context: dict, counts: dict, snapshot: dict) -> None:
    context["pr_snapshot"] = _snapshot_summary(snapshot)
    counts["snapshot_code"] = 0
    counts["impacted_symbols"] = 0
    counts["impacted_relations"] = 0
    if snapshot.get("status") != "ready" or not snapshot.get("pr_id"):
        context["code_graph_impact"] = {"related_files": context.get("related_files_from_logs") or [], "symbols": [], "relations": []}
        return

    repo = db.get(Repository, run.repo_id)
    snapshot_docs = _document_evidence(
        search_repository_code(
            repo,
            query,
            limit=8,
            checkout_path=snapshot.get("snapshot_path"),
            metadata={
                "checkout_kind": "pr_snapshot",
                "pr_id": str(snapshot["pr_id"]),
                "pr_number": snapshot.get("pr_number"),
                "branch": snapshot.get("branch"),
                "commit_sha": snapshot.get("commit_sha"),
            },
        )
        if repo and snapshot.get("snapshot_path")
        else [],
        "code",
    )
    context["code_references"] = _dedupe_evidence([*snapshot_docs, *(context.get("code_references") or [])], 10)
    pr_uuid = UUID(str(snapshot["pr_id"]))
    impact = _ci_code_graph_impact(db, pr_uuid, context.get("related_files_from_logs") or [])
    context["code_graph_impact"] = impact
    counts["snapshot_code"] = len(snapshot_docs)
    counts["impacted_symbols"] = len(impact["symbols"])
    counts["impacted_relations"] = len(impact["relations"])
    counts["code"] = len(context["code_references"])


def _build_ci_analysis_context(db: Session, run: WorkflowRun, conversation_id: UUID | None = None) -> tuple[str, dict, dict]:
    repo = db.get(Repository, run.repo_id)
    first_error = _first_error(run.logs_text)
    failed_jobs = _failed_jobs(run)
    failed_steps = [
        step
        for job in failed_jobs
        for step in job.get("failed_steps", [])
        if isinstance(step, dict)
    ]
    related_files = _related_files_from_logs(run.logs_text)
    code_query = "\n".join(
        filter(None, [run.name, run.conclusion or "", first_error or "", run.logs_text[:5000] if run.logs_text else ""])
    )
    semantic_query = sanitize_ci_log(
        "\n".join(filter(None, [run.name, first_error or "", run.logs_text[:3000] if run.logs_text else ""]))
    )
    code_docs = _document_evidence(
        search_repository_code(repo, code_query, limit=6) if repo else [],
        "code",
    )
    project_docs = _document_evidence(
        search_similar_documents(
            db,
            str(run.repo_id),
            semantic_query,
            limit=4,
            source_types=sorted(PROJECT_DOC_TYPES),
        ),
        "document",
    )
    if not project_docs:
        project_docs = _fallback_project_documents(db, run.repo_id)
    workflow_docs = _document_evidence(
        search_repository_code(repo, run.name, limit=4, relative_path=".github/workflows") if repo else [],
        "workflow",
    )
    recent_prs = [
        {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "author": pr.author,
            "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
        }
        for pr in (
            db.query(PullRequest)
            .filter(PullRequest.repo_id == run.repo_id)
            .order_by(PullRequest.updated_at.desc())
            .limit(5)
            .all()
        )
    ]
    chat_evidence = _conversation_evidence(db, run, conversation_id, semantic_query)
    context = {
        "repo_id": str(run.repo_id),
        "plan": _ci_plan(),
        "failed_jobs": failed_jobs,
        "failed_steps": failed_steps,
        "first_error": first_error,
        "related_files_from_logs": related_files,
        "code_references": code_docs,
        "workflow_references": workflow_docs,
        "project_documents": project_docs,
        "recent_prs": recent_prs,
        "conversation_evidence": chat_evidence,
    }
    counts = {
        "failed_jobs": len(failed_jobs),
        "failed_steps": len(failed_steps),
        "code": len(code_docs),
        "workflow": len(workflow_docs),
        "documents": len(project_docs),
        "recent_prs": len(recent_prs),
        "conversation": len(chat_evidence),
        "related_files": len(related_files),
    }
    return code_query, context, counts


def _persist_ci_analysis(
    db: Session,
    run: WorkflowRun,
    result: dict,
    model_name: str,
    conversation_id: UUID | None,
    evidence_counts: dict,
) -> AnalysisResult:
    analysis = AnalysisResult(
        target_type="workflow_run",
        target_id=run.id,
        analysis_type="ci_debug",
        input_snapshot={
            "run_id": str(run.id),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "evidence_counts": evidence_counts,
        },
        result_json=result,
        model_name=model_name,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis


@router.get("/repos/{repo_id}/workflow-runs", response_model=list[WorkflowRunResponse])
async def list_workflow_runs(repo_id: UUID, db: Session = Depends(get_db)) -> list[WorkflowRun]:
    return db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_id).order_by(WorkflowRun.created_at.desc()).all()


@router.get("/workflow-runs/{run_id}", response_model=WorkflowRunResponse)
async def get_workflow_run(run_id: UUID, db: Session = Depends(get_db)) -> WorkflowRun:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="未找到 workflow 运行")
    return run


@router.get("/workflow-runs/{run_id}/failed-jobs")
async def get_failed_jobs(run_id: UUID, db: Session = Depends(get_db)) -> dict:
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="未找到 workflow 运行")
    failed_jobs = [
        {
            **job,
            "failed_steps": [step for step in job.get("steps", []) if step.get("conclusion") == "failure"],
        }
        for job in (run.jobs or [])
        if job.get("conclusion") == "failure"
    ]
    return {"run_id": str(run.id), "workflow": run.name, "failed_jobs": failed_jobs}


async def _submit_ci(db, run_id, conversation_id):
    from app.services.conversations import ensure_conversation
    from app.services.agents.ci_debug_agent import CIDebugAgent
    run = await get_workflow_run(run_id, db)
    repo = db.get(Repository, run.repo_id)
    conversation = ensure_conversation(db, repo.id, conversation_id)
    return await CIDebugAgent().run({"run_id": str(run.id)}, {"db": db, "repo_id": repo.id, "conversation": conversation})


@router.post("/workflow-runs/{run_id}/analyze", response_model=InvestigationSubmission)
async def analyze_workflow_run(run_id: UUID, conversation_id: UUID | None = Query(None), db: Session = Depends(get_db)):
    return await _submit_ci(db, run_id, conversation_id)


@router.post("/workflow-runs/{run_id}/analyze/stream")
async def analyze_workflow_run_stream(run_id: UUID, request: Request, conversation_id: UUID | None = Query(None), db: Session = Depends(get_db)):
    from app.api.routes.review_runs import submission_stream
    submitted = await _submit_ci(db, run_id, conversation_id)
    return StreamingResponse(submission_stream(submitted, request), media_type="text/event-stream")


@router.get("/workflow-runs/{run_id}/analysis-run")
async def latest_ci_analysis_run(run_id: UUID, db: Session = Depends(get_db)):
    from app.services.review_runs import latest_target_run
    await get_workflow_run(run_id, db)
    return latest_target_run(db, "ci_debug", run_id)
