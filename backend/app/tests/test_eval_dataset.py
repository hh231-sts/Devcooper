import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import evaluations, knowledge, repos
from app.core.config import settings
from app.db.models import Base, Conversation, Document
from app.db.session import get_db
from app.services.evals.ragas_runner import _hard_rules
from app.services.evals.suites import DEFAULT_SUITE_DIR, load_eval_suite


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("prepare_eval_data", ROOT / "scripts" / "prepare_eval_data.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


@pytest.fixture
def dataset_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "milvus_enabled", False)
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    monkeypatch.setattr(settings, "embedding_model", "deterministic-demo")
    monkeypatch.setattr(settings, "embedding_dimensions", 128)
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "ragas_judge_api_key", None)
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(evaluations, "SessionLocal", factory)
    app = FastAPI()
    app.include_router(repos.router, prefix="/api/repos")
    app.include_router(knowledge.router, prefix="/api/repos")
    app.include_router(evaluations.router, prefix="/api/evals")

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, factory
    engine.dispose()


def test_bundled_dataset_and_default_loader_directory_match():
    dataset = prepare.load_dataset()
    assert DEFAULT_SUITE_DIR == ROOT / "docs" / "datas" / "evals"
    assert len(dataset["suite"]["cases"]) == 5
    assert len(dataset["files"]) == 4
    assert prepare.main(["--check"]) == 0


@pytest.mark.parametrize("broken", ["missing_source", "duplicate_id", "bad_regex", "bad_reference"])
def test_dataset_check_rejects_broken_distribution(tmp_path, broken):
    suite = json.loads(prepare.SUITE_PATH.read_text(encoding="utf-8"))
    if broken == "missing_source":
        suite["cases"][0]["source_file"] = "missing.md"
    elif broken == "duplicate_id":
        suite["cases"][1]["id"] = suite["cases"][0]["id"]
    elif broken == "bad_regex":
        suite["cases"][0]["answer_regex"] = "["
    else:
        suite["cases"][0]["reference"] = "wrong"
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(suite, ensure_ascii=False), encoding="utf-8")
    with pytest.raises((ValueError, __import__("re").error)):
        prepare.load_dataset(path)


def test_api_import_is_idempotent_and_source_mapping_is_repo_scoped(dataset_api):
    client, factory = dataset_api
    dataset = prepare.load_dataset()
    created = client.post("/api/repos/connect", json={"owner": "course-demo", "repo": "empty", "demo_mode": True}).json()
    empty_repo_id = created["repo_id"]
    before = client.get("/api/evals/suites", params={"repo_id": empty_repo_id}).json()[0]
    assert before["ready"] is False
    assert len(before["missing_sources"]) == 4
    rejected = client.post("/api/evals/rag/run", json={"repo_id": empty_repo_id, "suite_id": dataset["suite"]["name"]})
    assert rejected.status_code == 400
    prepared = prepare.prepare_dataset(client, dataset, repo_id=None, demo_repo=True)
    assert prepared["evaluation_set"]["ready"] is True
    assert prepared["evaluation_set"]["sha256"] == dataset["sha256"]
    again = prepare.prepare_dataset(client, dataset, repo_id=None, demo_repo=True)
    assert again["repo_id"] == prepared["repo_id"]
    assert all(row["duplicate"] for row in again["uploads"])
    assert client.get("/api/evals/suites", params={"repo_id": empty_repo_id}).json()[0]["ready"] is False
    with factory() as db:
        from uuid import UUID

        loaded = load_eval_suite(db, UUID(prepared["repo_id"]), dataset["suite"]["name"])
        count = db.query(Document).count()
        assert count >= 4
        for case in loaded.cases:
            assert case.expected_source_ids
            hard = _hard_rules(case, {"answer": case.reference}, [{"id": case.expected_source_ids[0]}], ["evidence"])
            assert hard["passed"] is True


def test_api_runs_all_bundled_cases_without_claiming_unscored_quality(dataset_api):
    client, factory = dataset_api
    prepared = prepare.prepare_dataset(client, prepare.load_dataset(), repo_id=None, demo_repo=True)
    result = prepare.run_evaluation(client, prepared, run_judge=False, wait_seconds=60)
    detail = result["result"]
    assert result["status"] not in {"queued", "running", "error", "no_cases"}
    assert len(detail["cases"]) == 5
    assert all(row["retrieved_contexts"] for row in detail["cases"])
    assert all(row["expected_source_ids"] for row in detail["cases"])
    assert detail["evaluation_set"]["sha256"] == prepared["evaluation_set"]["sha256"]
    assert detail["evaluation_complete"] is False
    assert detail["passed"] is False
    with factory() as db:
        assert db.query(Conversation).filter(Conversation.status == "evaluation").count() == 0


def test_import_rejects_same_filename_with_different_content(dataset_api):
    client, _ = dataset_api
    dataset = prepare.load_dataset()
    prepared = prepare.prepare_dataset(client, dataset, repo_id=None, demo_repo=True)
    response = client.post(f"/api/repos/{prepared['repo_id']}/knowledge/upload", files={
        "file": ("04_data_flow_and_sources.md", b"Outdated source content", "text/markdown"),
    })
    assert response.status_code == 200
    with pytest.raises(RuntimeError, match="different version"):
        prepare.prepare_dataset(client, dataset, repo_id=prepared["repo_id"], demo_repo=False)


def test_import_rejects_backend_with_a_different_suite_version(dataset_api):
    client, _ = dataset_api
    dataset = prepare.load_dataset()
    dataset["sha256"] = "not-the-backend-version"
    with pytest.raises(RuntimeError, match="SHA-256 differs"):
        prepare.prepare_dataset(client, dataset, repo_id=None, demo_repo=True)
