import asyncio
import copy
import json

import httpx
import pytest

from app.core.config import Settings, settings
from app.schemas.workflow import WorkflowObservation, WorkflowSpec
from app.services.agents.observer_agent import ObserverAgent
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.synthesis_agent import SynthesisAgent
from app.services.agents.workflow_models import execution_label, review_facts
from app.services.llm.client import LLMClient
from app.tests.test_workflow_models import StubLLM, context, observer_input, plan, result, review, spec


@pytest.fixture(autouse=True)
def default_rules_policy(monkeypatch):
    monkeypatch.setattr(settings, "workflow_synthesis_model_enabled", False)
    monkeypatch.setattr(settings, "workflow_replan_model_enabled", False)


def test_default_policy_and_explicit_rules_label():
    config = Settings(_env_file=None)
    assert not config.workflow_synthesis_model_enabled
    assert not config.workflow_replan_model_enabled
    assert config.workflow_observer_timeout_seconds == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("observation,status,expected", [
    ({"human_review_required": True}, "success", "需要人工复核"),
    ({}, "running", "任务执行不完整"),
    ({"findings": [{"finding_type": "ci_merge_gate", "severity": "blocker", "message": "CI failed"}]}, "success", "暂缓合并"),
    ({}, "success", "按 PR Agent 建议推进"),
])
async def test_default_synthesis_preserves_decision_and_five_sections_without_model(observation, status, expected):
    llm = StubLLM()
    inputs = observer_input([result(status=status)])
    actual = await SynthesisAgent(llm).run({**inputs, "observation": observation}, {})
    assert not llm.calls
    assert actual["model_execution"] == {"mode": "rules", "model": "", "reason": "rules_first_synthesis"}
    assert expected in actual["answer"]
    for section in ("What", "Why", "Tradeoffs", "Open Questions", "Next Actions"):
        assert section in actual["answer"]
    assert "规则处理" in actual["answer"] and "规则降级" not in actual["answer"]


@pytest.mark.asyncio
async def test_rule_replan_preserves_tasks_and_skips_even_opted_in_model_when_rule_adds_task(monkeypatch):
    monkeypatch.setattr(settings, "workflow_replan_model_enabled", True)
    previous = spec()
    previous.max_parallel_tasks = 2
    inputs = {**observer_input(workflow=previous), "observation": {"findings": [
        {"finding_type": "semantic_conflict", "severity": "warning", "message": "Need history"}]}}
    llm = StubLLM()
    actual = WorkflowSpec.model_validate(await PlannerAgent(llm).replan(inputs, context()))
    assert not llm.calls
    assert actual.planning.mode == "rules"
    assert actual.tasks[0] == previous.tasks[0]
    assert actual.max_parallel_tasks == 2
    assert [task.task_type for task in actual.tasks] == ["pr_review", "rag_search"]


@pytest.mark.asyncio
async def test_default_replan_does_not_call_model_without_rule_recovery():
    llm = StubLLM()
    actual = await PlannerAgent(llm).replan({**observer_input(), "observation": {}}, context())
    assert not llm.calls
    assert len(actual["tasks"]) == 1
    assert actual["planning"]["mode"] == "rules"


@pytest.mark.asyncio
async def test_rule_replan_keeps_initial_model_provenance_in_final_report():
    planned = await PlannerAgent(StubLLM(plan("pr_review"))).run({"message": "Review PR"}, context())
    replanned = await PlannerAgent(StubLLM()).replan({"spec": planned, "task_results": [], "observation": {}}, context())
    assert replanned["planning"]["mode"] == "rules"
    assert replanned["initial_planning"]["mode"] == "llm"
    output = await SynthesisAgent(StubLLM()).run({"spec": replanned, "task_results": [], "observation": {}}, {})
    assert "Planner=大模型" in output["answer"]


def test_review_catalog_excludes_counts_locators_secrets_and_truncated_fields():
    actual = review_facts([result(citations=[{"id": "one-source"}], output={
        "api_key": "never send", "large": "x" * 2000, "behavior": "anonymous accepted"})])
    serialized = json.dumps(actual)
    assert "evidence_count" not in serialized
    assert "citations" not in serialized and "one-source" not in serialized
    assert "never send" not in serialized and "[truncated]" not in serialized
    assert "anonymous accepted" in serialized


@pytest.mark.asyncio
async def test_metadata_count_mismatch_is_not_sent_to_model_or_flagged():
    llm = StubLLM(review())
    actual = await ObserverAgent(llm).run(observer_input([result(evidence_count=7, citations=[{"id": "one-pr"}])]), {})
    assert actual["model_review"]["mode"] == "llm"
    assert not actual["findings"]
    assert "evidence_count" not in json.dumps(llm.calls[0][1])


def quoted_finding(**overrides):
    return {"finding_type": "semantic_conflict", "severity": "blocker", "message": "summary and behavior disagree",
            "task_ids": ["pr_review"], "recommendation": "Compare implementation with requirement",
            "evidence": [{"fact_id": "f1", "quote": "Reviewed change"},
                         {"fact_id": "f2", "quote": "needs review"}], **overrides}


@pytest.mark.asyncio
async def test_observer_records_verifiable_fact_quotes():
    actual = await ObserverAgent(StubLLM(review(findings=[quoted_finding()]))).run(observer_input(), {})
    assert actual["model_review"]["mode"] == "llm"
    finding = actual["findings"][0]
    assert finding["source"] == "model"
    assert {item["path"] for item in finding["evidence"]} == {"/summary", "/output/merge_recommendation"}
    assert all(item["quote"] in item["text"] for item in finding["evidence"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown_fact", "forged_quote", "duplicate_fact", "unknown_task"])
async def test_forged_review_evidence_requires_human_without_inventing_domain_conflict(kind):
    finding = quoted_finding()
    if kind == "unknown_fact":
        finding["evidence"][0]["fact_id"] = "invented"
    elif kind == "forged_quote":
        finding["evidence"][0]["quote"] = "model invented a source"
    elif kind == "duplicate_fact":
        finding["evidence"][1] = copy.deepcopy(finding["evidence"][0])
    else:
        finding["task_ids"] = ["invented"]
    actual = await ObserverAgent(StubLLM(review(findings=[finding]))).run(observer_input(), {})
    assert actual["model_review"]["reason"] == "invalid_fact_reference"
    assert actual["human_review_required"]
    assert not actual["findings"]


@pytest.mark.asyncio
async def test_unsubstantiated_model_human_flag_without_finding_does_not_add_gate():
    actual = await ObserverAgent(StubLLM(review(human_review_required=True))).run(observer_input(), {})
    assert actual["model_review"]["mode"] == "llm"
    assert not actual["findings"]
    assert not actual["human_review_required"]


@pytest.mark.asyncio
async def test_observer_timeout_cancels_request_and_requires_human_without_fake_finding(monkeypatch):
    cancelled = asyncio.Event()

    class SlowLLM(StubLLM):
        async def chat_json(self, prompt, payload):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

    monkeypatch.setattr(settings, "workflow_observer_timeout_seconds", .01)
    observed = await ObserverAgent(SlowLLM()).run(observer_input(), {})
    assert cancelled.is_set()
    assert observed["model_review"]["reason"] == "timeout"
    assert observed["human_review_required"]
    assert not observed["findings"]
    output = await SynthesisAgent(StubLLM()).run({**observer_input(), "observation": observed}, {})
    assert "需要人工复核" in output["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,expected_fast", [("https://api.deepseek.com", True), ("https://example.com/v1", False)])
async def test_observer_fast_client_options_do_not_change_other_providers(monkeypatch, base_url, expected_fast):
    monkeypatch.setattr(settings, "llm_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_base_url", base_url)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(review())}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    actual = await ObserverAgent().run(observer_input(), {})
    assert actual["model_review"]["mode"] == "llm"
    assert requests[0]["max_tokens"] == 1800
    assert ("thinking" in requests[0]) == expected_fast
    planner_client = LLMClient()
    assert planner_client.json_max_tokens is None and not planner_client.json_fast_review
