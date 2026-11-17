import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.models import AgentTaskRun, AgentWorkflowRun, ReviewConclusion, ReviewEvent, ReviewEvidence, ReviewMessage, ReviewSnapshot
from app.db import session as database
from app.db.session import get_db
from app.services.review_runs import ACTIVE, current_result, event, latest_conclusions, row_dict

router = APIRouter()


def require_run(db, run_id):
    run = db.get(AgentWorkflowRun, run_id)
    if run is None:
        raise HTTPException(404, "运行不存在。")
    return run


def event_data(item):
    return {"id": item.id, "kind": item.kind, "task_id": item.task_id, "payload": item.payload,
            "created_at": item.created_at.isoformat()}


@router.get("/{run_id}")
def get_review_run(run_id: uuid.UUID, db: Session = Depends(get_db)):
    run = require_run(db, run_id)
    tasks = db.query(AgentTaskRun).filter_by(workflow_id=run.id).all()
    snapshot = db.get(ReviewSnapshot, run.snapshot_id) if run.snapshot_id else None
    materials = snapshot.materials if snapshot else {}
    conclusions = latest_conclusions(db, run.id)
    return {"id": str(run.id), "goal": run.goal, "status": run.status, "cancel_requested": run.cancel_requested,
            "spec": run.spec_json, "observation": run.observation_json, "final_answer": run.final_answer,
            "active_seconds": run.active_seconds, "metrics": run.metrics, "phase": run.checkpoint_json.get("phase"),
            "assistant_message_id": str(run.assistant_message_id) if run.assistant_message_id else None,
            "snapshot": {"id": str(run.snapshot_id) if snapshot else None,
                         "issue": materials.get("issue"), "code_sha": materials.get("code_sha"),
                         "pr": {k: v for k, v in (materials.get("pr") or {}).items() if k not in {"diff", "files", "comments"}},
                         "ci": {k: v for k, v in (materials.get("ci") or {}).items() if k not in {"logs_text", "jobs", "pull_requests_json"}},
                         "ci_missing": materials.get("ci_missing") if not materials.get("ci") else None},
            "tasks": [{"id": str(task.id), "task_id": task.task_id, "agent_name": task.agent_name,
                       "task_type": task.task_type, "objective": task.input_json.get("objective"), "status": task.status,
                       "budget": task.budget_json, "result_version": task.result_version, "result": current_result(task, conclusions).model_dump(mode="json") if task.result_json else None,
                       "code_sha": task.input_json.get("code_sha"),
                       "error": task.error, "phase": task.checkpoint_json.get("phase"),
                       "pending_call": task.checkpoint_json.get("pending_call")} for task in tasks],
            "conclusions": conclusions,
            "conclusion_history": [{"task_id": item.task_id, **item.content} for item in db.query(ReviewConclusion).filter_by(workflow_id=run.id).order_by(ReviewConclusion.version).all()],
            "evidence": [row_dict(item, ["id", "task_id", "source", "version", "location", "excerpt"]) for item in db.query(ReviewEvidence).filter_by(workflow_id=run.id).all()],
            "messages": [{"id": item.id, "request_id": item.request_id, "sender": item.sender, "recipient": item.recipient,
                          "kind": item.kind, "round": item.round, "content": item.content} for item in db.query(ReviewMessage).filter_by(workflow_id=run.id).order_by(ReviewMessage.id).all()],
            "events": [event_data(item) for item in db.query(ReviewEvent).filter_by(workflow_id=run.id).order_by(ReviewEvent.id).all()]}


@router.get("/{run_id}/events")
async def subscribe_review_events(run_id: uuid.UUID, request: Request, after: int = Query(0, ge=0),
                                  last_event_id: int | None = Header(default=None), db: Session = Depends(get_db)):
    require_run(db, run_id)
    db.rollback()

    return StreamingResponse(review_event_stream(run_id, request, max(after, last_event_id or 0)), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def review_event_stream(run_id, request, cursor=0):
    while not await request.is_disconnected():
        with database.SessionLocal() as session:
            run = session.get(AgentWorkflowRun, run_id)
            if run is None:
                return
            status = run.status
            rows = session.query(ReviewEvent).filter(ReviewEvent.workflow_id == run_id, ReviewEvent.id > cursor).order_by(ReviewEvent.id).limit(100).all()
            records = [event_data(item) for item in rows]
        for record in records:
            cursor = record["id"]
            yield f"id: {cursor}\nevent: review_event\ndata: {json.dumps(record, ensure_ascii=False)}\n\n"
        if status not in ACTIVE and len(records) < 100:
            yield f"event: settled\ndata: {json.dumps({'status': status, 'last_event_id': cursor})}\n\n"
            return
        if not records:
            yield ": heartbeat\n\n"
        await asyncio.sleep(1)


async def submission_stream(submitted, request):
    yield f"event: submitted\ndata: {json.dumps(submitted, ensure_ascii=False)}\n\n"
    async for record in review_event_stream(uuid.UUID(submitted["workflow_run_id"]), request):
        yield record


@router.post("/{run_id}/cancel")
def cancel_review_run(run_id: uuid.UUID, db: Session = Depends(get_db)):
    run = require_run(db, run_id)
    if run.status in ACTIVE and not run.cancel_requested:
        run.cancel_requested = True
        event(db, run.id, "cancel_requested", {"status": run.status})
        db.commit()
    return {"id": str(run.id), "status": run.status, "cancel_requested": run.cancel_requested}
