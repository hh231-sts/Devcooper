import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import chat as chat_routes
from app.core.config import settings
from app.db.models import ChatMessage, ChatSession, Conversation, Repository
from app.db.session import Base
from app.schemas.chat import ChatRequest
from app.services.agents.chat_agent import ChatAgent
from app.services.chat_memory import ContextAssembler, MemoryTools, MessageStore, ProgressiveContextManager
from app.services.context_compression import MICROCOMPACT_MARKER, dumps_compact, estimate_tokens, microcompact_messages


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _scope(db):
    repo = Repository(owner="acme", name="history", full_name="acme/history")
    db.add(repo)
    db.flush()
    conversation = Conversation(repo_id=repo.id, title="History tools")
    db.add(conversation)
    db.flush()
    return repo, conversation


def _row(db, repo, conversation, role, content, offset, **kwargs):
    row = ChatMessage(
        repo_id=repo.id, conversation_id=conversation.id, role=role, content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset), **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def _events(content, call_id="call_read", tool_name="workspace.read_file", **kwargs):
    return [
        {"type": "tool_use", "id": call_id, "tool_name": tool_name, "arguments": {"path": "backend/app/auth.py"}},
        {"type": "tool_result", "tool_use_id": call_id, "tool_name": tool_name, "status": "success", "content": content, **kwargs},
    ]


def _turn(db, repo, conversation, events, offset=0, *, sealed=True, metadata=True):
    user = _row(db, repo, conversation, "user", "Inspect auth", offset)
    assistant = _row(
        db, repo, conversation, "assistant", "Keep token refresh in middleware.", offset + 1,
        meta={"agent_events": events} if metadata else {},
    )
    session = None
    if sealed:
        session = ChatSession(
            repo_id=repo.id, conversation_id=conversation.id,
            user_message_id=user.id, assistant_message_id=assistant.id,
            route="workspace_read_file", digest="auth inspection", status="sealed",
            transcript_json={"agent_events": events, "tool_use_results": events},
        )
        db.add(session)
        db.flush()
    return user, assistant, session


def _small_window(monkeypatch):
    monkeypatch.setattr(settings, "context_dynamic_model_window_enabled", False)
    monkeypatch.setattr(settings, "context_model_window_tokens", 4096)
    monkeypatch.setattr(settings, "context_max_input_tokens", 4096)
    monkeypatch.setattr(settings, "context_reserved_response_tokens", 512)
    monkeypatch.setattr(settings, "context_recent_ratio", 0.75)
    monkeypatch.setattr(settings, "context_microcompact_keep_recent_messages", 2)


@pytest.mark.asyncio
async def test_sealed_results_reach_prompt_and_remain_exactly_recoverable(db, monkeypatch):
    _small_window(monkeypatch)
    repo, conversation = _scope(db)
    content = {"answer_preview": "auth token refresh evidence. " * 3000}
    events = _events(content)
    user, assistant, session = _turn(db, repo, conversation, events, metadata=False)
    original_transcript = copy.deepcopy(session.transcript_json)
    _row(db, repo, conversation, "user", "Continue checking auth", 2)

    assembled = ContextAssembler(db).assemble(repo, conversation, "Continue checking auth")
    stats = assembled.compression_stats["microcompact"]
    assert stats["compacted_messages"] == 1
    assert stats["estimated_tokens_saved"] > 0
    text = "\n".join(item["content"] for item in assembled.history_messages)
    assert MICROCOMPACT_MARKER in text
    assert str(session.id) in text
    assert "workspace.read_file" in text
    assert "auth token refresh evidence" in text
    assert assembled.history_messages[-2]["content"] == assistant.content
    assert assembled.compression_stats["history_coverage"]["complete_and_exact"] is False
    agent = ChatAgent(model=None)
    prompt = agent._tool_calling_user_prompt({"message": "Continue checking auth", "context": {
        "server_messages": assembled.history_messages, "compression_stats": assembled.compression_stats,
    }})
    assert MICROCOMPACT_MARKER in prompt
    assert content["answer_preview"] not in prompt

    recovered = ""
    offset = 0
    while True:
        page = await MemoryTools(db, repo, conversation).read_session_events(
            {"session_id": str(session.id), "include_transcript": True, "offset": offset, "max_chars": 20000}, {},
        )
        result = page["results"][0]
        recovered += result["snippet"]
        offset = result["metadata"]["next_offset"]
        if offset is None:
            break
    assert dumps_compact(original_transcript) in recovered
    db.expire_all()
    assert db.get(ChatSession, session.id).transcript_json == original_transcript
    assert db.get(ChatMessage, assistant.id).content == "Keep token refresh in middleware."
    assert db.query(ChatMessage).count() == 3


def test_metadata_only_results_are_projected_without_relabeling_final_answer(db, monkeypatch):
    _small_window(monkeypatch)
    repo, conversation = _scope(db)
    events = _events("historical file contents. " * 3000)
    _user, assistant, _session = _turn(db, repo, conversation, events, sealed=False)
    _row(db, repo, conversation, "user", "Continue", 2)
    assembled = ContextAssembler(db).assemble(repo, conversation, "Continue")
    assert assembled.compression_stats["microcompact"]["compacted_messages"] == 1
    projected = MessageStore(db).context_projection(repo.id, conversation.id, [assistant])
    assert projected[0]["role"] == "tool_result"
    assert projected[-1]["role"] == "assistant"
    assert projected[-1]["meta"].get("subtype") is None
    assert assistant.meta["agent_events"] == events


@pytest.mark.asyncio
async def test_native_tool_rows_are_included_and_recoverable(db, monkeypatch):
    _small_window(monkeypatch)
    repo, conversation = _scope(db)
    _row(db, repo, conversation, "user", "Inspect file", 0)
    tool = _row(db, repo, conversation, "tool_result", "native result. " * 3000, 1, route="workspace.read_file")
    _row(db, repo, conversation, "assistant", "Checked file", 2)
    _row(db, repo, conversation, "user", "Continue", 3)
    original = tool.content
    assembled = ContextAssembler(db).assemble(repo, conversation, "Continue")
    text = "\n".join(item["content"] for item in assembled.history_messages)
    assert MICROCOMPACT_MARKER in text
    assert str(tool.id) in text
    assert assembled.compression_stats["microcompact"]["compacted_messages"] == 1
    result = await MemoryTools(db, repo, conversation).get_thread_context(
        {"message_id": str(tool.id), "max_chars": 16000}, {},
    )
    assert result["results"][0]["snippet"] == original[:16000]
    assert db.get(ChatMessage, tool.id).content == original


@pytest.mark.parametrize("kind", ["recent", "short", "nonregenerable", "error", "unknown"])
def test_microcompact_protects_ineligible_and_recent_results(db, monkeypatch, kind):
    _small_window(monkeypatch)
    repo, conversation = _scope(db)
    kwargs = {}
    if kind == "nonregenerable":
        kwargs["meta"] = {"regenerable": False}
    if kind == "error":
        kwargs["status"] = "error"
    tool_name = "action.execute" if kind == "unknown" else "workspace.read_file"
    events = _events("short" if kind == "short" else "original evidence. " * 3000, tool_name=tool_name, **kwargs)
    _turn(db, repo, conversation, events)
    if kind != "recent":
        _row(db, repo, conversation, "user", "Continue", 2)
    store = MessageStore(db)
    raw = store.context_projection(repo.id, conversation.id, store.recent(repo.id, conversation.id))
    compacted, stats = microcompact_messages(raw, "evidence", keep_recent=2)
    assert stats["compacted_messages"] == 0
    assert [item["content"] for item in compacted] == [item["content"] for item in raw]


def test_results_are_not_duplicated_between_event_indexes_or_reused_call_ids(db):
    repo, conversation = _scope(db)
    _turn(db, repo, conversation, _events("first result"), offset=0)
    _turn(db, repo, conversation, _events("second result"), offset=2)
    store = MessageStore(db)
    raw = store.context_projection(repo.id, conversation.id, store.recent(repo.id, conversation.id))
    results = [item for item in raw if item["role"] == "tool_result"]
    assert len(results) == 2
    assert "first result" in results[0]["content"]
    assert "second result" in results[1]["content"]


def test_native_results_are_not_replayed_from_assistant_events(db):
    repo, conversation = _scope(db)
    _row(db, repo, conversation, "user", "Inspect", 0)
    _row(db, repo, conversation, "tool_result", "native payload", 1, route="workspace.read_file", meta={"tool_use_id": "call_read"})
    _row(db, repo, conversation, "assistant", "Done", 2, meta={"agent_events": _events("duplicate payload")})
    store = MessageStore(db)
    raw = store.context_projection(repo.id, conversation.id, store.recent(repo.id, conversation.id))
    assert [item["content"] for item in raw if item["role"] == "tool_result"] == ["native payload"]


def test_projection_obeys_compact_boundary_and_conversation_scope(db):
    repo, conversation = _scope(db)
    _turn(db, repo, conversation, _events("EXCLUDED-OLD-TOOL"), offset=0)
    _user, preserved, _session = _turn(db, repo, conversation, _events("PRESERVED-TOOL"), offset=2)
    _row(db, repo, conversation, "system", "summary", 4, meta={
        "subtype": "compact_boundary", "preserved_message_ids": [str(preserved.id)],
    })
    _row(db, repo, conversation, "user", "Continue", 5)
    other = Conversation(repo_id=repo.id, title="Other thread")
    db.add(other)
    db.flush()
    db.add(ChatSession(
        repo_id=repo.id, conversation_id=other.id, assistant_message_id=preserved.id,
        route="chat", digest="", status="sealed", transcript_json={"agent_events": _events("EXCLUDED-OTHER-THREAD")},
    ))
    db.flush()
    store = MessageStore(db)
    raw = store.context_projection(repo.id, conversation.id, store.recent(repo.id, conversation.id))
    text = dumps_compact(raw)
    assert "PRESERVED-TOOL" in text
    assert "EXCLUDED-OLD-TOOL" not in text
    assert "EXCLUDED-OTHER-THREAD" not in text


@pytest.mark.asyncio
async def test_microcompact_sufficient_uses_tool_pressure_without_summary_model(db, monkeypatch):
    _small_window(monkeypatch)
    repo, conversation = _scope(db)
    _turn(db, repo, conversation, _events("old result. " * 3000))
    _row(db, repo, conversation, "user", "Continue", 2)

    class NoSummaryModel:
        async def chat_text(self, *args, **kwargs):
            raise AssertionError("microcompact is sufficient; summary model must not run")

    manager = ProgressiveContextManager(db, llm=NoSummaryModel())
    result = await manager.ensure_headroom(repo, conversation, "Continue")
    assert result["reason"] == "microcompact_sufficient"
    assert result["attempted"] is False
    assert result["pressure"]["raw_segment_estimated_tokens"] > 4096
    assert result["microcompact"]["compacted_messages"] == 1
    assert db.query(ChatMessage).filter(ChatMessage.role == "system").count() == 0


@pytest.mark.asyncio
async def test_formal_summary_and_post_compact_estimate_include_projected_tools(db):
    repo, conversation = _scope(db)
    _user, assistant, _session = _turn(db, repo, conversation, _events("AUTH-ERROR TimeoutError: auth.py failed. " * 100))

    class SummaryModel:
        async def chat_text(self, system, user):
            assert "AUTH-ERROR" in user
            return "Auth inspection is complete; retain the error and investigate next."

    manager = ProgressiveContextManager(db, llm=SummaryModel())
    summary, _mode, _report = await manager._summary_for(repo, conversation, [assistant], "Continue", 1800)
    assert summary
    estimate = manager._post_compact_tokens("summary", [assistant], current_message="Continue")
    assert estimate > estimate_tokens(assistant.content) + 300


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_real_chat_turn_seals_tools_and_next_model_call_receives_microcompact(db, monkeypatch, stream):
    repo, conversation = _scope(db)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "milvus_enabled", False)
    monkeypatch.setattr(settings, "context_dynamic_model_window_enabled", True)
    monkeypatch.setattr(settings, "context_model_window_tokens", 100_000)
    raw_result = "AuthMiddleware refreshes expired tokens. " * 2000

    class Model:
        def __init__(self):
            self.prompts = []

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.prompts.append(str(messages[-1].content))
            if len(self.prompts) == 1:
                return AIMessage(content="", tool_calls=[{
                    "name": "workspace__read_file", "args": {"path": "backend/app/auth.py"}, "id": "actual_read",
                }])
            return AIMessage(content="Keep token refresh in middleware.")

    model = Model()

    async def read_file(_input, _context):
        return {"route": "workspace_read_file", "answer": raw_result, "citations": []}

    monkeypatch.setattr(chat_routes, "ChatAgent", lambda: ChatAgent(model=model))
    monkeypatch.setattr(chat_routes, "_build_conversation_tools", lambda *_: {"workspace.read_file": read_file})

    async def request(message):
        payload = ChatRequest(repo_id=repo.id, conversation_id=conversation.id, message=message)
        if not stream:
            return await chat_routes._conversation_response(payload, db)
        events = []
        async for chunk in chat_routes._conversation_sse(payload, db):
            event_name = ""
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_name = line[7:]
                if line.startswith("data: "):
                    events.append({"event": event_name, "data": json.loads(line[6:])})
        return next(item["data"] for item in events if item["event"] == "final")

    first = await request("Check token refresh behavior")
    first_message = db.get(ChatMessage, UUID(str(first["message_id"])))
    sealed = db.query(ChatSession).filter(ChatSession.assistant_message_id == first_message.id).one()
    before = copy.deepcopy(sealed.transcript_json)
    assert any(event.get("type") == "tool_result" for event in before["agent_events"])
    _small_window(monkeypatch)
    second = await request("Continue checking auth")
    assert second["compression_stats"]["microcompact"]["compacted_messages"] == 1
    assert second["progressive_compaction"]["reason"] == "microcompact_sufficient"
    assert MICROCOMPACT_MARKER in model.prompts[-1]
    assert raw_result not in model.prompts[-1]
    assert str(sealed.id) in model.prompts[-1]
    db.expire_all()
    assert db.get(ChatSession, sealed.id).transcript_json == before
    assert db.get(ChatMessage, first_message.id).content == "Keep token refresh in middleware."
