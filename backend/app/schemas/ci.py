from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.analysis import CIDebugOutput


class WorkflowRunResponse(BaseModel):
    id: UUID
    repo_id: UUID
    name: str
    status: str
    conclusion: str | None = None
    html_url: str | None = None
    commit_sha: str | None = None
    run_attempt: int | None = None
    workflow_path: str | None = None
    pull_requests_json: list[dict] = []
    jobs: list[dict] = []
    logs_text: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class CIAnalysisResponse(BaseModel):
    analysis_id: UUID | str
    result: CIDebugOutput
