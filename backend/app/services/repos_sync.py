from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decrypt_token
from app.db.concurrency import run_blocking
from app.db.models import Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun
from app.schemas.repos import RepoSyncRequest, RepoSyncResponse
from app.services.github.actions import get_workflow_run_logs, list_workflow_run_jobs, list_workflow_runs
from app.services.github.issues import list_issues
from app.services.github.pull_requests import list_pull_request_files, list_pull_request_review_comments, list_pull_requests
from app.services.repo_sync_guard import repository_sync_guard


def parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def login_of(user: dict | None) -> str | None:
    return user.get("login") if user else None


@dataclass
class SyncSnapshot:
    issues: list[dict] = field(default_factory=list)
    pull_requests: list[tuple[dict, list[dict] | None, list[dict] | None]] = field(default_factory=list)
    workflow_runs: list[tuple[dict, list[dict] | None, str | None]] = field(default_factory=list)


def _load_repository(engine: Engine, repo_id: UUID) -> Repository:
    with Session(engine) as reader:
        repo = reader.get(Repository, repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="未找到仓库")
        reader.expunge(repo)
        return repo


async def sync_repository(db: Session, repo: Repository, payload: RepoSyncRequest) -> RepoSyncResponse:
    engine, repo_id = db.get_bind(), repo.id
    async with repository_sync_guard(engine, repo_id):
        await run_blocking(db.commit)
        target = await run_blocking(_load_repository, engine, repo_id)
        snapshot, result = await _fetch_snapshot(target, payload)
        await run_blocking(_persist_snapshot, engine, repo_id, snapshot, result, payload.limit)
        db.expire_all()
        return result


async def _fetch_snapshot(repo: Repository, payload: RepoSyncRequest) -> tuple[SyncSnapshot, RepoSyncResponse]:
    if repo.provider not in {"github", "github_compatible"}:
        raise HTTPException(status_code=400, detail=f"{repo.provider} sync is not implemented yet")

    synced = {"issues": 0, "pull_requests": 0, "workflow_runs": 0}
    token = decrypt_token(repo.github_token_encrypted, settings.token_encryption_key) if repo.github_token_encrypted else None
    base_url = repo.api_base_url
    sources = {key: {"status": "skipped", "count": 0, "complete": False, "pages": 0} for key in synced}
    errors: list[str] = []
    snapshot = SyncSnapshot()

    async def fetch(source: str, loader, *args, detail: str | None = None, **kwargs):
        try:
            items = await loader(*args, **kwargs)
        except (httpx.HTTPError, ValueError) as exc:
            reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            message = f"{source}{'/' + detail if detail else ''}: {reason}"
            errors.append(message)
            sources[source]["status"] = "partial" if detail else "failed"
            sources[source]["complete"] = False
            return None
        if detail is None:
            complete = getattr(items, "complete", True)
            sources[source] = {
                "status": ("completed" if items else "empty") if complete else "partial",
                "count": len(items), "complete": complete, "pages": getattr(items, "pages", 1),
            }
        return items

    if payload.sync_issues:
        remote_issues = await fetch("issues", list_issues, repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        snapshot.issues = list(remote_issues or [])
        synced["issues"] = len(snapshot.issues)

    if payload.sync_pull_requests:
        remote_prs = await fetch("pull_requests", list_pull_requests, repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        for item in remote_prs or []:
            number = item.get("number", 0)
            files = await fetch("pull_requests", list_pull_request_files, repo.owner, repo.name, number, token=token, base_url=base_url, detail=f"#{number}/files")
            comments = await fetch("pull_requests", list_pull_request_review_comments, repo.owner, repo.name, number, token=token, base_url=base_url, detail=f"#{number}/comments")
            snapshot.pull_requests.append((item, files, comments))
        synced["pull_requests"] = len(snapshot.pull_requests)

    if payload.sync_workflow_runs:
        remote_runs = await fetch("workflow_runs", list_workflow_runs, repo.owner, repo.name, token=token, limit=payload.limit, base_url=base_url)
        for item in remote_runs or []:
            run_id = item.get("id")
            jobs = logs = None
            if run_id:
                jobs = await fetch("workflow_runs", list_workflow_run_jobs, repo.owner, repo.name, run_id, token=token, base_url=base_url, detail=f"{run_id}/jobs")
                if item.get("conclusion") == "failure":
                    logs = await fetch("workflow_runs", get_workflow_run_logs, repo.owner, repo.name, run_id, token=token, base_url=base_url, detail=f"{run_id}/logs")
            snapshot.workflow_runs.append((item, jobs, logs))
        synced["workflow_runs"] = len(snapshot.workflow_runs)

    requested = [item for item in sources.values() if item["status"] != "skipped"]
    if not requested:
        status = "skipped"
    elif all(item["status"] == "failed" for item in requested):
        status = "failed"
    elif any(item["status"] in {"failed", "partial"} for item in requested):
        status = "partial"
    else:
        status = "completed" if any(synced.values()) else "empty"
    messages = {
        "completed": "已完成所选数据源的全部分页同步。",
        "empty": "所选数据源请求成功，但未返回记录；不能据此判断仓库健康。",
        "partial": "同步不完整：存在请求失败或数量上限，请查看各数据源状态。",
        "failed": "同步失败，未获取到有效的新数据；保留已有数据。",
        "skipped": "未选择数据源，没有执行同步。",
    }
    return snapshot, RepoSyncResponse(repo_id=repo.id, status=status, synced=synced, sources=sources, errors=errors, message=messages[status])


def _persist_snapshot(engine: Engine, repo_id: UUID, snapshot: SyncSnapshot, result: RepoSyncResponse, limit: int | None) -> None:
    # No network calls are allowed in this short, atomic publication phase.
    with Session(engine) as db, db.begin():
        if engine.dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            db.execute(text("SET LOCAL statement_timeout = '30s'"))
        repo = db.get(Repository, repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="未找到仓库")
        for item in snapshot.issues:
            issue = db.query(Issue).filter(Issue.repo_id == repo.id, Issue.github_issue_id == item.get("id")).one_or_none()
            if issue is None:
                issue = Issue(repo_id=repo.id, github_issue_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(issue)
            issue.number = item.get("number", issue.number)
            issue.title = item.get("title") or issue.title
            issue.body = item.get("body")
            issue.state = item.get("state") or "open"
            issue.labels = [label.get("name") for label in item.get("labels", []) if label.get("name")]
            issue.author = login_of(item.get("user"))
            issue.assignees = [login_of(user) for user in item.get("assignees", []) if login_of(user)]
            issue.created_at = parse_github_datetime(item.get("created_at"))
            issue.updated_at = parse_github_datetime(item.get("updated_at"))
            issue.closed_at = parse_github_datetime(item.get("closed_at"))

        for item, files, review_comments in snapshot.pull_requests:
            pr = db.query(PullRequest).filter(PullRequest.repo_id == repo.id, PullRequest.github_pr_id == item.get("id")).one_or_none()
            if pr is None:
                pr = PullRequest(repo_id=repo.id, github_pr_id=item.get("id"), number=item.get("number", 0), title=item.get("title") or "")
                db.add(pr)
                db.flush()
            pr.number = item.get("number", pr.number)
            pr.title = item.get("title") or pr.title
            pr.body = item.get("body")
            pr.state = item.get("state") or "open"
            pr.author = login_of(item.get("user"))
            pr.base_branch = (item.get("base") or {}).get("ref")
            pr.head_branch = (item.get("head") or {}).get("ref")
            pr.merged_at = parse_github_datetime(item.get("merged_at"))
            pr.created_at = parse_github_datetime(item.get("created_at"))
            pr.updated_at = parse_github_datetime(item.get("updated_at"))

            if files is not None:
                db.query(PRFile).filter(PRFile.pr_id == pr.id).delete()
            for file_item in files or []:
                db.add(
                    PRFile(
                        pr_id=pr.id,
                        filename=file_item.get("filename") or "",
                        status=file_item.get("status") or "modified",
                        additions=file_item.get("additions") or 0,
                        deletions=file_item.get("deletions") or 0,
                        patch=file_item.get("patch"),
                    )
                )
            if review_comments is not None:
                db.query(PRReviewComment).filter(PRReviewComment.pr_id == pr.id).delete()
            for comment_item in review_comments or []:
                db.add(
                    PRReviewComment(
                        pr_id=pr.id,
                        github_comment_id=comment_item.get("id"),
                        body=comment_item.get("body"),
                        path=comment_item.get("path"),
                        line=comment_item.get("line") or comment_item.get("original_line"),
                        author=login_of(comment_item.get("user")),
                        created_at=parse_github_datetime(comment_item.get("created_at")),
                        updated_at=parse_github_datetime(comment_item.get("updated_at")),
                    )
                )

        for item, jobs, logs in snapshot.workflow_runs:
            run = db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id, WorkflowRun.github_run_id == item.get("id")).one_or_none()
            if run is None:
                run = WorkflowRun(repo_id=repo.id, github_run_id=item.get("id"), name=item.get("name") or "workflow")
                db.add(run)
            run.name = item.get("name") or run.name
            run.status = item.get("status") or "unknown"
            run.conclusion = item.get("conclusion")
            run.html_url = item.get("html_url")
            run.created_at = parse_github_datetime(item.get("created_at"))
            run.updated_at = parse_github_datetime(item.get("updated_at"))
            if run.github_run_id:
                if jobs is not None:
                    run.jobs = [
                        {
                            "id": job.get("id"),
                            "name": job.get("name"),
                            "status": job.get("status"),
                            "conclusion": job.get("conclusion"),
                            "html_url": job.get("html_url"),
                            "started_at": job.get("started_at"),
                            "completed_at": job.get("completed_at"),
                            "steps": [
                                {
                                    "name": step.get("name"),
                                    "status": step.get("status"),
                                    "conclusion": step.get("conclusion"),
                                    "number": step.get("number"),
                                }
                                for step in job.get("steps", [])
                            ],
                        }
                        for job in jobs
                    ]
            if run.conclusion == "failure" and run.github_run_id:
                if logs is not None:
                    run.logs_text = logs

        now = datetime.now(timezone.utc)
        if result.status in {"completed", "empty"}:
            repo.last_sync_at = now
        repo.sync_metadata = {**result.model_dump(mode="json"), "attempted_at": now.isoformat(), "limit": limit}


async def sync_repository_by_id(repo_id: UUID, payload: RepoSyncRequest) -> RepoSyncResponse | None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        repo = await run_blocking(db.get, Repository, repo_id)
        if repo is None:
            return None
        return await sync_repository(db, repo, payload)
    finally:
        await run_blocking(db.close)
