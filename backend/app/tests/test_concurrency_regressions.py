import asyncio
import os
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes import chat, evaluations, repos
from app.core.config import settings
from app.db.concurrency import run_blocking
from app.db.models import (
    Base, Conversation, Document, EvalRun, EvidenceItem, Issue, KnowledgeBaseConfig,
    PRFile, PRReviewComment, PullRequest, RecallEvent, Repository,
)
from app.db.session import get_db
from app.schemas.chat import ChatRequest
from app.schemas.repos import RepoSyncRequest
from app.services import repos_sync
from app.services.chat_memory import EvidenceStore
from app.services.evals.ragas_runner import RagEvalCase, _run_chatagent_case
from app.services.repo_sync_guard import repository_sync_guard


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "milvus_enabled", False)
    monkeypatch.setattr(settings, "llm_api_key", None)
    schema = None
    if request.param == "postgresql":
        url = os.getenv("DEVCOOPER_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set DEVCOOPER_TEST_POSTGRES_URL to run real PostgreSQL lock regressions")
        schema = "concurrency_" + uuid.uuid4().hex
        engine = create_engine(url, connect_args={"application_name": schema, "connect_timeout": 3}, execution_options={"schema_translate_map": {None: schema}})
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    else:
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'concurrency.db'}", connect_args={"check_same_thread": False, "timeout": 3})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        repo = Repository(owner="acme", name="demo", full_name="acme/demo", description="Repository facts")
        db.add(repo)
        db.flush()
        issue = Issue(repo_id=repo.id, github_issue_id=1, number=1, title="Original issue", state="open")
        pr = PullRequest(repo_id=repo.id, github_pr_id=2, number=2, title="Original PR", state="open")
        conversation = Conversation(repo_id=repo.id, title="Normal chat")
        db.add_all([issue, pr, conversation])
        db.flush()
        db.add_all([
            PRFile(pr_id=pr.id, filename="old.py", status="modified"),
            PRReviewComment(pr_id=pr.id, body="Original review"),
            Document(repo_id=repo.id, source_type="project_doc", source_id=uuid.uuid4(), title="Retry policy", content="Retries use an idempotency key.", meta={"marker": ["original"]}),
            EvidenceItem(repo_id=repo.id, conversation_id=None, source_type="repository", source_id=str(repo.id), title="Shared evidence", content="Keep shared evidence", meta={}),
            KnowledgeBaseConfig(repo_id=repo.id, embedding_provider="deterministic", embedding_model="test", embedding_dimensions=32, rerank_enabled=False, score_threshold_enabled=False),
        ])
        db.commit()
        ids = repo.id, conversation.id
    try:
        yield engine, factory, ids
    finally:
        if schema:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def stub_github(monkeypatch):
    async def issues(*_args, **_kwargs):
        return [{"id": 1, "number": 1, "title": "New issue", "state": "open"}]

    async def prs(*_args, **_kwargs):
        return [{"id": 2, "number": 2, "title": "New PR", "state": "open"}]

    async def files(*_args, **_kwargs):
        return [{"filename": "new.py", "status": "modified"}]

    async def comments(*_args, **_kwargs):
        return [{"id": 3, "body": "New review"}]

    async def runs(*_args, **_kwargs):
        return []

    for name, function in [("list_issues", issues), ("list_pull_requests", prs), ("list_pull_request_files", files), ("list_pull_request_review_comments", comments), ("list_workflow_runs", runs)]:
        monkeypatch.setattr(repos_sync, name, function)


@pytest.mark.asyncio
async def test_eval_and_normal_chat_complete_without_mutating_shared_evidence(database, monkeypatch):
    engine, factory, (repo_id, conversation_id) = database
    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingAgent:
        async def run(self, payload, context):
            # Exercise a real tool that flushes RecallEvent while the trial runs.
            await context["tools"]["rag.search_similar_documents"]({"query": "idempotency"}, context)
            await context["tools"]["devcooper_search_evidence"]({"query": "idempotency"}, context)
            await context["tools"]["devcooper_get_thread_context"]({}, context)
            await context["tools"]["devcooper_read_session_events"]({}, context)
            entered.set()
            await finish.wait()
            return {"answer": "Use an idempotency key", "tool_calls": [], "citations": []}

    class ChatAnswer:
        async def run(self, payload, context):
            return {"answer": "Normal chat completed", "route": "conversation", "tool_calls": [], "citations": []}

    async def seal(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat, "ChatAgent", ChatAnswer)
    monkeypatch.setattr(chat.SessionSealer, "seal", seal)
    async def no_production_mcp(*_args, **_kwargs):
        raise AssertionError("Evaluation must not open the production MCP database")
    monkeypatch.setattr(chat.StdioMcpClient, "call_tool", no_production_mcp)
    with factory() as source, factory() as normal:
        task = asyncio.create_task(_run_chatagent_case(source, source.get(Repository, repo_id), RagEvalCase(question="Retry policy?", reference="idempotency", expected_source_ids=[]), limit=5, agent_factory=WaitingAgent))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not source.in_transaction()
            result = await asyncio.wait_for(chat._conversation_response(ChatRequest(repo_id=repo_id, conversation_id=conversation_id, message="Hello"), normal), 3)
            assert result["answer"] == "Normal chat completed"
            with factory() as observer:
                assert observer.query(Conversation).filter(Conversation.status == "evaluation").count() == 0
                assert observer.query(EvidenceItem).filter(EvidenceItem.conversation_id.is_(None)).one().content == "Keep shared evidence"
                assert observer.query(RecallEvent).filter(RecallEvent.conversation_id != conversation_id).count() == 0
        finally:
            finish.set()
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_eval_sandbox_discards_all_trial_writes(database, outcome):
    engine, factory, (repo_id, _) = database
    entered = asyncio.Event()

    class MutatingAgent:
        async def run(self, payload, context):
            assert context["evaluation_mode"] is True
            entered.set()
            if outcome == "error":
                raise RuntimeError("model failed")
            if outcome == "cancel":
                await asyncio.Event().wait()
            return {"answer": "done"}

    from app.services.evals import ragas_runner
    original = ragas_runner._run_sandbox_case

    async def mutate_private_db(db, repo, *args, **kwargs):
        assert db.get_bind() is not engine
        assert db.query(KnowledgeBaseConfig).one().embedding_model == "test"
        doc = db.query(Document).one()
        doc.title = "Private trial title"
        doc.meta["marker"].append("private")
        db.add(EvalRun(name="private", result_json={}))
        db.commit()
        return await original(db, repo, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ragas_runner, "_run_sandbox_case", mutate_private_db)
        with factory() as source:
            task = asyncio.create_task(_run_chatagent_case(source, source.get(Repository, repo_id), RagEvalCase(question="Q", reference="A", expected_source_ids=[]), limit=5, agent_factory=MutatingAgent))
            await asyncio.wait_for(entered.wait(), 3)
            if outcome == "cancel":
                task.cancel()
            if outcome == "success":
                assert (await task)["answer"] == "done"
            else:
                with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
                    await task
    with factory() as observer:
        assert observer.query(Document).one().title == "Retry policy"
        assert observer.query(Document).one().meta == {"marker": ["original"]}
        assert observer.query(Conversation).filter(Conversation.status == "evaluation").count() == 0
        assert observer.query(EvalRun).count() == 0


def test_evidence_rebuild_is_scoped_to_one_conversation(database):
    _, factory, (repo_id, conversation_id) = database
    with factory() as db:
        repo = db.get(Repository, repo_id)
        first = db.get(Conversation, conversation_id)
        second = Conversation(repo_id=repo_id, title="Another chat")
        db.add(second)
        db.flush()
        EvidenceStore(db).rebuild_repo(repo, first)
        db.commit()
        first_ids = {row.id for row in db.query(EvidenceItem).filter(EvidenceItem.conversation_id == first.id)}
        EvidenceStore(db).rebuild_repo(repo, second)
        db.commit()
        assert first_ids == {row.id for row in db.query(EvidenceItem).filter(EvidenceItem.conversation_id == first.id)}
        EvidenceStore(db).rebuild_repo(repo)
        db.commit()
        assert first_ids == {row.id for row in db.query(EvidenceItem).filter(EvidenceItem.conversation_id == first.id)}
        assert db.query(EvidenceItem).filter(EvidenceItem.conversation_id == second.id).count() > 0


@pytest.mark.asyncio
async def test_sync_fetches_every_detail_before_any_write(database, monkeypatch):
    engine, factory, (repo_id, _) = database
    stub_github(monkeypatch)
    writes = []

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    original = repos_sync.list_pull_request_review_comments

    async def delayed_comments(*args, **kwargs):
        assert writes == []
        with factory() as observer:
            assert observer.query(PRFile).one().filename == "old.py"
            if engine.dialect.name == "postgresql":
                assert observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = current_setting('application_name') AND state = 'idle in transaction' AND pid <> pg_backend_pid()")) == 0
        return await original(*args, **kwargs)

    monkeypatch.setattr(repos_sync, "list_pull_request_review_comments", delayed_comments)
    event.listen(engine, "before_cursor_execute", record)
    try:
        with factory() as db:
            result = await repos_sync.sync_repository(db, db.get(Repository, repo_id), RepoSyncRequest())
        assert result.status == "completed" and writes
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.asyncio
async def test_manual_refresh_conflicts_with_scheduled_sync_without_blocking_health(database, monkeypatch):
    engine, factory, (repo_id, _) = database
    stub_github(monkeypatch)
    from app.db import session
    monkeypatch.setattr(session, "SessionLocal", factory)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def delayed_issues(*_args, **_kwargs):
        entered.set()
        await finish.wait()
        return []

    monkeypatch.setattr(repos_sync, "list_issues", delayed_issues)
    app = FastAPI()
    app.include_router(repos.router, prefix="/api/repos")

    def get_test_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = get_test_db

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    task = asyncio.create_task(repos_sync.sync_repository_by_id(repo_id, RepoSyncRequest()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await asyncio.wait_for(client.post(f"/api/repos/{repo_id}/sync", json={}), 1)
            assert response.status_code == 409
            assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
            listing = await asyncio.wait_for(client.get("/api/repos"), 1)
            assert listing.status_code == 200
            assert listing.json()[0]["full_name"] == "acme/demo"
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["exception", "cancel"])
async def test_sync_guard_released_after_failure_or_cancellation(database, monkeypatch, fault):
    _, factory, (repo_id, _) = database
    stub_github(monkeypatch)
    entered = asyncio.Event()

    async def fail(*_args, **_kwargs):
        entered.set()
        if fault == "cancel":
            await asyncio.Event().wait()
        raise RuntimeError("network failed")

    monkeypatch.setattr(repos_sync, "list_issues", fail)
    with factory() as db:
        task = asyncio.create_task(repos_sync.sync_repository(db, db.get(Repository, repo_id), RepoSyncRequest()))
        await asyncio.wait_for(entered.wait(), 3)
        if fault == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if fault == "cancel" else RuntimeError):
            await task
        assert db.query(PRFile).one().filename == "old.py"
        stub_github(monkeypatch)
        assert (await repos_sync.sync_repository(db, db.get(Repository, repo_id), RepoSyncRequest())).status == "completed"


@pytest.mark.asyncio
async def test_publication_failure_rolls_back_the_entire_snapshot(database, monkeypatch):
    engine, factory, (repo_id, _) = database
    stub_github(monkeypatch)

    def fail(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("INSERT INTO") and "pr_review_comments" in statement:
            raise RuntimeError("publication failed")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with factory() as db:
            with pytest.raises(RuntimeError, match="publication failed"):
                await repos_sync.sync_repository(db, db.get(Repository, repo_id), RepoSyncRequest())
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with factory() as observer:
        assert observer.query(Issue).one().title == "Original issue"
        assert observer.query(PRFile).one().filename == "old.py"
        assert observer.query(PRReviewComment).one().body == "Original review"
        assert observer.get(Repository, repo_id).sync_metadata is None


@pytest.mark.asyncio
async def test_database_lock_wait_does_not_block_the_server_loop(database, monkeypatch):
    engine, factory, (repo_id, _) = database
    stub_github(monkeypatch)
    waiting = threading.Event()
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(update(Issue).where(Issue.repo_id == repo_id).values(title="Held by another writer"))

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("UPDATE") and "issues" in statement:
            waiting.set()

    event.listen(engine, "before_cursor_execute", record)
    with factory() as db:
        task = asyncio.create_task(repos_sync.sync_repository(db, db.get(Repository, repo_id), RepoSyncRequest()))
        try:
            assert await run_blocking(waiting.wait, 3)
            # This coroutine must run while the database worker is waiting.
            await asyncio.sleep(0.05)
            assert not task.done()
            transaction.rollback()
            assert (await asyncio.wait_for(task, 3)).status == "completed"
        finally:
            if transaction.is_active:
                transaction.rollback()
            blocker.close()
            event.remove(engine, "before_cursor_execute", record)
            await task


@pytest.mark.asyncio
async def test_cancelled_publication_keeps_mutex_until_worker_finishes(database, monkeypatch):
    _, factory, (repo_id, _) = database
    stub_github(monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    original = repos_sync._persist_snapshot

    def delayed(*args):
        entered.set()
        assert finish.wait(3)
        return original(*args)

    monkeypatch.setattr(repos_sync, "_persist_snapshot", delayed)
    with factory() as db, factory() as other:
        task = asyncio.create_task(repos_sync.sync_repository(db, db.get(Repository, repo_id), RepoSyncRequest()))
        try:
            assert await run_blocking(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0.01)
            with pytest.raises(HTTPException) as busy:
                await repos_sync.sync_repository(other, other.get(Repository, repo_id), RepoSyncRequest())
            assert busy.value.status_code == 409
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        monkeypatch.setattr(repos_sync, "_persist_snapshot", original)
        assert (await repos_sync.sync_repository(other, other.get(Repository, repo_id), RepoSyncRequest())).status == "completed"


@pytest.mark.asyncio
async def test_postgres_mutex_excludes_an_independent_backend_process(database):
    engine, _, (repo_id, _) = database
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock contract")
    program = """
import asyncio, sys
from fastapi import HTTPException
from sqlalchemy import create_engine
from uuid import UUID
from app.services.repo_sync_guard import repository_sync_guard
async def main():
    engine = create_engine(sys.argv[1])
    try:
        async with repository_sync_guard(engine, UUID(sys.argv[2])):
            raise AssertionError('Another process entered the same repository sync')
    except HTTPException as error:
        assert error.status_code == 409
    finally:
        engine.dispose()
asyncio.run(main())
"""
    async with repository_sync_guard(engine, repo_id):
        result = await run_blocking(subprocess.run, [sys.executable, "-c", program, engine.url.render_as_string(hide_password=False), str(repo_id)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
    # A subsequent independent engine can enter after release.
    other = create_engine(engine.url)
    try:
        async with repository_sync_guard(other, repo_id):
            pass
    finally:
        other.dispose()


@pytest.mark.asyncio
async def test_background_eval_uses_its_own_worker_session_and_does_not_block_loop(database, monkeypatch):
    _, factory, (repo_id, _) = database
    with factory() as db:
        run = EvalRun(name="background", result_json={"status": "queued"})
        db.add(run)
        db.commit()
        eval_id = str(run.id)
    main_thread = threading.get_ident()

    async def slow_run(db, *_args, **_kwargs):
        assert threading.get_ident() != main_thread
        time.sleep(0.15)
        return {"status": "passed", "passed": True}

    monkeypatch.setattr(evaluations, "SessionLocal", factory)
    monkeypatch.setattr(evaluations, "run_rag_quality_eval", slow_run)
    task = asyncio.create_task(evaluations._execute_rag_eval_job(eval_id, str(repo_id), None, 5, False, {}))
    ticks = 0
    while not task.done():
        await asyncio.sleep(0.01)
        ticks += 1
    await task
    assert ticks >= 5
    with factory() as observer:
        assert observer.get(EvalRun, uuid.UUID(eval_id)).result_json["status"] == "passed"


@pytest.mark.asyncio
async def test_background_eval_cancellation_persists_interrupted_state(database, monkeypatch):
    _, factory, (repo_id, _) = database
    entered = threading.Event()
    with factory() as db:
        run = EvalRun(name="cancelled", result_json={"status": "queued"})
        db.add(run)
        db.commit()
        eval_id = str(run.id)

    async def wait_for_model(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(evaluations, "SessionLocal", factory)
    monkeypatch.setattr(evaluations, "run_rag_quality_eval", wait_for_model)
    task = asyncio.create_task(evaluations._execute_rag_eval_job(eval_id, str(repo_id), None, 5, False, {}))
    assert await run_blocking(entered.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    with factory() as observer:
        result = observer.get(EvalRun, uuid.UUID(eval_id)).result_json
        assert result["status"] == "error"
        assert result["progress"]["stage"] == "interrupted"


@pytest.mark.asyncio
async def test_different_repositories_can_sync_concurrently(database, monkeypatch):
    _, factory, (repo_id, _) = database
    stub_github(monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()
    with factory() as db:
        other = Repository(owner="acme", name="other", full_name="acme/other")
        db.add(other)
        db.commit()
        other_id = other.id

    async def wait_for_first(_owner, name, **_kwargs):
        if name == "demo":
            entered.set()
            await finish.wait()
        return []

    monkeypatch.setattr(repos_sync, "list_issues", wait_for_first)
    with factory() as first, factory() as second:
        task = asyncio.create_task(repos_sync.sync_repository(first, first.get(Repository, repo_id), RepoSyncRequest()))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            result = await asyncio.wait_for(repos_sync.sync_repository(second, second.get(Repository, other_id), RepoSyncRequest()), 3)
            assert result.status == "completed"
            assert not task.done()
        finally:
            finish.set()
            await task


@pytest.mark.asyncio
async def test_chat_database_wait_keeps_event_loop_responsive(database, monkeypatch):
    engine, factory, (repo_id, conversation_id) = database
    with factory() as setup:
        EvidenceStore(setup).rebuild_repo(setup.get(Repository, repo_id), setup.get(Conversation, conversation_id))
        setup.commit()
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(update(EvidenceItem).where(EvidenceItem.conversation_id == conversation_id).values(title="Held evidence"))
    waiting = threading.Event()

    def record(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("INSERT INTO") and "chat_messages" in statement:
            waiting.set()

    class ChatAnswer:
        async def run(self, *_args):
            return {"answer": "done", "route": "conversation", "tool_calls": [], "citations": []}

    async def seal(*_args, **_kwargs):
        pass

    monkeypatch.setattr(chat, "ChatAgent", ChatAnswer)
    monkeypatch.setattr(chat.SessionSealer, "seal", seal)
    event.listen(engine, "before_cursor_execute", record)
    with factory() as db:
        task = asyncio.create_task(chat._conversation_response(ChatRequest(repo_id=repo_id, conversation_id=conversation_id, message="Hello"), db))
        try:
            assert await run_blocking(waiting.wait, 3)
            await asyncio.sleep(0.05)
            assert not task.done()
            transaction.rollback()
            assert (await asyncio.wait_for(task, 3))["answer"] == "done"
        finally:
            if transaction.is_active:
                transaction.rollback()
            blocker.close()
            event.remove(engine, "before_cursor_execute", record)
            await task


def test_eval_snapshot_excludes_other_projects_and_is_consistent(database):
    engine, factory, (repo_id, _) = database
    from app.services.evals.sandbox import evaluation_session

    with factory() as db:
        neighbor = Repository(owner="acme", name="neighbor", full_name="acme/neighbor")
        db.add(neighbor)
        db.flush()
        db.add(Document(repo_id=neighbor.id, source_type="project_doc", source_id=uuid.uuid4(), title="Other project", content="Do not copy", meta={}))
        db.commit()
    changed = False

    def publish_between_snapshot_queries(_connection, _cursor, statement, *_args):
        nonlocal changed
        if not changed and engine.dialect.name == "postgresql" and statement.lstrip().upper().startswith("SELECT") and ".issues" in statement:
            changed = True
            with factory() as writer, writer.begin():
                writer.execute(update(Issue).where(Issue.repo_id == repo_id).values(title="Updated issue"))
                writer.execute(update(PullRequest).where(PullRequest.repo_id == repo_id).values(title="Updated PR"))

    event.listen(engine, "after_cursor_execute", publish_between_snapshot_queries)
    try:
        with factory() as source, evaluation_session(source, repo_id) as sandbox:
            assert sandbox.query(Repository).count() == 1
            assert sandbox.query(Document).one().title == "Retry policy"
            assert sandbox.query(Issue).one().title == "Original issue"
            assert sandbox.query(PullRequest).one().title == "Original PR"
    finally:
        event.remove(engine, "after_cursor_execute", publish_between_snapshot_queries)
    if engine.dialect.name == "postgresql":
        assert changed
        with factory() as observer:
            assert observer.query(PullRequest).filter(PullRequest.repo_id == repo_id).one().title == "Updated PR"
