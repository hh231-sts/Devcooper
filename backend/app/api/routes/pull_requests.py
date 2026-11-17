from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.db.models import AnalysisResult, CodeRelation, CodeSymbol, Conversation, Document, PullRequest, Repository, WorkflowRun
from app.db.session import get_db
from app.schemas.pull_requests import PullRequestResponse
from app.schemas.workflow import InvestigationSubmission
from app.services.chat_memory import ContextAssembler
from app.services.code_search import search_repository_code
from app.services.rag.retrieval import search_similar_documents
from app.services.worktree_manager import cleanup_pr_snapshots, prepare_pr_worktree

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


def _conversation_evidence(db: Session, pr: PullRequest, conversation_id: UUID | None, query: str) -> list[dict]:
    if not conversation_id:
        return []
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repo_id != pr.repo_id:
        return []
    repo = db.get(Repository, pr.repo_id)
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


def _pr_payload(pr: PullRequest) -> dict:
    return {
        "id": str(pr.id),
        "number": pr.number,
        "title": pr.title,
        "body": pr.body,
        "state": pr.state,
        "author": pr.author,
        "base_branch": pr.base_branch,
        "head_branch": pr.head_branch,
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "files": [
            {
                "filename": file.filename,
                "status": file.status,
                "additions": file.additions,
                "deletions": file.deletions,
                "patch": file.patch,
            }
            for file in pr.files
        ],
        "comments": [
            {"body": comment.body, "path": comment.path, "line": comment.line, "author": comment.author}
            for comment in pr.review_comments
        ],
    }


def _pr_review_plan() -> list[str]:
    return [
        "理解 PR 标题、描述、变更文件和关联上下文。",
        "按模块拆分 diff 风险。",
        "读取相关源代码、测试文件和项目文档。",
        "检查 CI 状态和失败信号。",
        "检索团队约定、历史对话和 review comments。",
        "形成合入建议和 review comments。",
    ]


def _build_pr_analysis_context(db: Session, pr: PullRequest, conversation_id: UUID | None = None) -> tuple[str, dict, dict]:
    repo = db.get(Repository, pr.repo_id)
    file_names = [file.filename for file in pr.files]
    diff_text = "\n".join(f"{file.filename}\n{file.patch or ''}" for file in pr.files[:12])
    code_query = "\n".join(filter(None, [pr.title, pr.body or "", " ".join(file_names), diff_text[:5000]]))
    review_text = "\n".join(comment.body or "" for comment in pr.review_comments)
    semantic_query = "\n".join(filter(None, [pr.title, pr.body or "", review_text[:3000]]))
    code_docs = _document_evidence(
        search_repository_code(repo, code_query, limit=6) if repo else [],
        "code",
    )
    project_docs = _document_evidence(
        search_similar_documents(
            db,
            str(pr.repo_id),
            semantic_query,
            limit=4,
            source_types=sorted(PROJECT_DOC_TYPES),
        ),
        "document",
    )
    if not project_docs:
        project_docs = _fallback_project_documents(db, pr.repo_id)
    related_issues = _document_evidence(
        search_similar_documents(db, str(pr.repo_id), semantic_query, "issue", 4),
        "issue",
    )
    chat_evidence = _conversation_evidence(db, pr, conversation_id, semantic_query)
    recent_runs = (
        db.query(WorkflowRun)
        .filter(WorkflowRun.repo_id == pr.repo_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(8)
        .all()
    )
    failed_runs = [run for run in recent_runs if run.conclusion == "failure"]
    team_members = [
        {
            "name": (doc.meta or {}).get("name") or doc.title.replace("团队成员：", "").replace("Team member: ", ""),
            "role": (doc.meta or {}).get("role") or "",
            "strengths": (doc.meta or {}).get("strengths") or "",
            "techStack": (doc.meta or {}).get("tech_stack") or "",
        }
        for doc in db.query(Document).filter(Document.repo_id == pr.repo_id, Document.source_type == "team_member").all()
    ]
    total_additions = sum(file.additions or 0 for file in pr.files)
    total_deletions = sum(file.deletions or 0 for file in pr.files)
    sensitive_files = [
        name
        for name in file_names
        if any(token in name.lower() for token in ["auth", "security", "migration", "schema", "payment", "permission", "config", "ci", "workflow"])
    ]
    context = {
        "repo_id": str(pr.repo_id),
        "plan": _pr_review_plan(),
        "diff_stats": {
            "files": len(pr.files),
            "additions": total_additions,
            "deletions": total_deletions,
            "sensitive_files": sensitive_files,
        },
        "code_references": code_docs,
        "project_documents": project_docs,
        "related_issues": related_issues,
        "conversation_evidence": chat_evidence,
        "review_comments": _pr_payload(pr)["comments"],
        "ci_summary": {
            "recent_runs": len(recent_runs),
            "failed_runs": len(failed_runs),
            "failed": [
                {"name": run.name, "conclusion": run.conclusion, "logs_text": (run.logs_text or "")[:260]}
                for run in failed_runs[:4]
            ],
        },
        "team_members": team_members,
    }
    counts = {
        "files": len(pr.files),
        "comments": len(pr.review_comments),
        "code": len(code_docs),
        "documents": len(project_docs),
        "issues": len(related_issues),
        "conversation": len(chat_evidence),
        "failed_ci": len(failed_runs),
        "team_members": len(team_members),
    }
    return code_query, context, counts


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
    }


def _prepare_pr_snapshot_context(db: Session, pr: PullRequest, enabled: bool) -> dict:
    if not enabled:
        return {"status": "skipped", "message": "未请求 PR 分支快照。"}
    repo = db.get(Repository, pr.repo_id)
    if repo is None:
        return {"status": "missing_repo", "message": "未找到仓库。"}
    return prepare_pr_worktree(db, repo, pr)


def _pr_code_graph_impact(db: Session, pr: PullRequest) -> dict:
    changed_paths = list(dict.fromkeys(file.filename for file in pr.files if file.filename))
    symbol_query = db.query(CodeSymbol).filter(CodeSymbol.repo_id == pr.repo_id, CodeSymbol.pr_id == pr.id)
    if changed_paths:
        symbol_query = symbol_query.filter(CodeSymbol.path.in_(changed_paths))
    symbols = symbol_query.order_by(CodeSymbol.path.asc(), CodeSymbol.start_line.asc()).limit(30).all()
    symbol_names = [symbol.name for symbol in symbols]
    relation_query = db.query(CodeRelation).filter(CodeRelation.repo_id == pr.repo_id, CodeRelation.pr_id == pr.id)
    relation_filters = []
    if changed_paths:
        relation_filters.append(CodeRelation.path.in_(changed_paths))
    if symbol_names:
        relation_filters.extend([CodeRelation.source_name.in_(symbol_names), CodeRelation.target_name.in_(symbol_names)])
    if relation_filters:
        relation_query = relation_query.filter(or_(*relation_filters))
    relations = relation_query.limit(60).all()
    return {
        "changed_paths": changed_paths[:30],
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


def _augment_pr_context_with_snapshot(db: Session, pr: PullRequest, query: str, context: dict, counts: dict, snapshot: dict) -> None:
    context["pr_snapshot"] = _snapshot_summary(snapshot)
    counts["snapshot_code"] = 0
    counts["impacted_symbols"] = 0
    counts["impacted_relations"] = 0
    if snapshot.get("status") != "ready":
        context["code_graph_impact"] = {"changed_paths": [file.filename for file in pr.files], "symbols": [], "relations": []}
        return

    repo = db.get(Repository, pr.repo_id)
    snapshot_docs = _document_evidence(
        search_repository_code(
            repo,
            query,
            limit=8,
            checkout_path=snapshot.get("snapshot_path"),
            metadata={
                "checkout_kind": "pr_snapshot",
                "pr_id": str(pr.id),
                "pr_number": pr.number,
                "branch": snapshot.get("branch"),
                "commit_sha": snapshot.get("commit_sha"),
            },
        )
        if repo and snapshot.get("snapshot_path")
        else [],
        "code",
    )
    context["code_references"] = _dedupe_evidence([*snapshot_docs, *(context.get("code_references") or [])], 10)
    impact = _pr_code_graph_impact(db, pr)
    context["code_graph_impact"] = impact
    counts["snapshot_code"] = len(snapshot_docs)
    counts["impacted_symbols"] = len(impact["symbols"])
    counts["impacted_relations"] = len(impact["relations"])
    counts["code"] = len(context["code_references"])


def _persist_pr_analysis(
    db: Session,
    pr: PullRequest,
    result: dict,
    model_name: str,
    conversation_id: UUID | None,
    evidence_counts: dict,
) -> AnalysisResult:
    analysis = AnalysisResult(
        target_type="pull_request",
        target_id=pr.id,
        analysis_type="pr_review",
        input_snapshot={
            "pr_id": str(pr.id),
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


@router.get("/repos/{repo_id}/pull-requests", response_model=list[PullRequestResponse])
async def list_pull_requests(repo_id: UUID, db: Session = Depends(get_db)) -> list[PullRequest]:
    return (
        db.query(PullRequest)
        .options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments))
        .filter(PullRequest.repo_id == repo_id)
        .order_by(PullRequest.number.desc())
        .all()
    )


@router.get("/pull-requests/{pr_id}", response_model=PullRequestResponse)
async def get_pull_request(pr_id: UUID, db: Session = Depends(get_db)) -> PullRequest:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files), selectinload(PullRequest.review_comments)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    return pr


async def _submit_pr(db, pr_id, conversation_id):
    from app.services.conversations import ensure_conversation
    from app.services.agents.pr_review_agent import PRReviewAgent
    pr = await get_pull_request(pr_id, db)
    repo = db.get(Repository, pr.repo_id)
    conversation = ensure_conversation(db, repo.id, conversation_id)
    return await PRReviewAgent().run({"pr_id": str(pr.id)}, {"db": db, "repo_id": repo.id, "conversation": conversation})


@router.post("/pull-requests/{pr_id}/analyze", response_model=InvestigationSubmission)
async def analyze_pull_request(pr_id: UUID, conversation_id: UUID | None = Query(None), db: Session = Depends(get_db)):
    return await _submit_pr(db, pr_id, conversation_id)


@router.post("/pull-requests/{pr_id}/analyze/stream")
async def analyze_pull_request_stream(pr_id: UUID, request: Request, conversation_id: UUID | None = Query(None), db: Session = Depends(get_db)):
    from app.api.routes.review_runs import submission_stream
    submitted = await _submit_pr(db, pr_id, conversation_id)
    return StreamingResponse(submission_stream(submitted, request), media_type="text/event-stream")


@router.get("/pull-requests/{pr_id}/analysis-run")
async def latest_pr_analysis_run(pr_id: UUID, db: Session = Depends(get_db)):
    from app.services.review_runs import latest_target_run
    await get_pull_request(pr_id, db)
    return latest_target_run(db, "pr_review", pr_id)


@router.post("/pull-requests/{pr_id}/snapshot")
async def prepare_pull_request_snapshot(pr_id: UUID, db: Session = Depends(get_db)) -> dict:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    return _prepare_pr_snapshot_context(db, pr, enabled=True)


@router.post("/pull-requests/{pr_id}/worktree")
async def prepare_pull_request_worktree(pr_id: UUID, db: Session = Depends(get_db)) -> dict:
    pr = db.query(PullRequest).options(selectinload(PullRequest.files)).filter(PullRequest.id == pr_id).one_or_none()
    if pr is None:
        raise HTTPException(status_code=404, detail="未找到 PR")
    return _prepare_pr_snapshot_context(db, pr, enabled=True)


@router.post("/repos/{repo_id}/pr-snapshots/cleanup")
async def cleanup_pull_request_snapshots(
    repo_id: UUID,
    max_age_hours: int = Query(default=168, ge=1, le=24 * 90),
    db: Session = Depends(get_db),
) -> dict:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="未找到仓库")
    return cleanup_pr_snapshots(db, repo, max_age_hours=max_age_hours)


@router.post("/pull-requests/{pr_id}/review-checklist", response_model=InvestigationSubmission)
async def review_checklist(pr_id: UUID, db: Session = Depends(get_db)):
    return await _submit_pr(db, pr_id, None)
