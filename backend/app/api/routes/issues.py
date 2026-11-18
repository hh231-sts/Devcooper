from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.models import Issue
from app.db.session import get_db
from app.schemas.issues import IssueResponse
from app.schemas.workflow import InvestigationSubmission
from app.services.agents.issue_agent import IssueAgent
from app.services.conversations import ensure_conversation
from app.services.review_snapshot import search_materials

router = APIRouter()


def require_issue(db, issue_id):
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    return issue


async def submit_analysis(db, issue_id, conversation_id):
    issue = require_issue(db, issue_id)
    conversation = ensure_conversation(db, issue.repo_id, conversation_id)
    return await IssueAgent().run({"issue_id": str(issue_id)}, {"db": db, "repo_id": issue.repo_id, "conversation": conversation})


@router.get("/repos/{repo_id}/issues", response_model=list[IssueResponse])
async def list_issues(repo_id: UUID, db: Session = Depends(get_db)):
    return db.query(Issue).filter_by(repo_id=repo_id).order_by(Issue.number.desc()).all()


@router.get("/issues/{issue_id}", response_model=IssueResponse)
async def get_issue(issue_id: UUID, db: Session = Depends(get_db)):
    return require_issue(db, issue_id)


@router.post("/issues/{issue_id}/analyze", response_model=InvestigationSubmission)
async def analyze_issue(issue_id: UUID, conversation_id: UUID | None = Query(None), db: Session = Depends(get_db)):
    return await submit_analysis(db, issue_id, conversation_id)


@router.post("/issues/{issue_id}/analyze/stream")
async def analyze_issue_stream(issue_id: UUID, request: Request, conversation_id: UUID | None = Query(None), db: Session = Depends(get_db)):
    from app.api.routes.review_runs import submission_stream
    submitted = await submit_analysis(db, issue_id, conversation_id)
    return StreamingResponse(submission_stream(submitted, request), media_type="text/event-stream")


@router.get("/issues/{issue_id}/analysis-run")
async def latest_analysis_run(issue_id: UUID, db: Session = Depends(get_db)):
    require_issue(db, issue_id)
    from app.services.review_runs import latest_target_run
    return latest_target_run(db, "issue_analysis", issue_id)


@router.get("/issues/{issue_id}/similar")
async def similar_issues(issue_id: UUID, db: Session = Depends(get_db)):
    from app.services.review_runs import freeze_issue
    issue = require_issue(db, issue_id)
    candidates = [freeze_issue(item) for item in db.query(Issue).filter(Issue.repo_id == issue.repo_id, Issue.id != issue.id).all()]
    return search_materials(candidates, issue.title)
