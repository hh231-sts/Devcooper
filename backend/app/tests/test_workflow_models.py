import asyncio
import copy
import json

import httpx
import pytest

from app.core.config import settings
from app.schemas.workflow import WorkflowClaim, WorkflowFinding, WorkflowObservation, WorkflowSpec, WorkflowTask, WorkflowTaskResult
from app.services.agents.observer_agent import ObserverAgent
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.synthesis_agent import SynthesisAgent
from app.services.agents.workflow_models import PLAN_PROMPT, REVIEW_PROMPT, SYNTHESIS_PROMPT, PlanProposal, request_workflow_model, task_evidence
from app.services.agents.workflow_orchestrator import WorkflowOrchestrator
from app.services.llm.client import LLMClient


class StubLLM:
    api_key = "test-only"
    model = "test-model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_json(self, prompt, payload):
        self.calls.append((prompt, copy.deepcopy(payload)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


@pytest.fixture(autouse=True)
def opt_in_optional_models_for_model_contract_tests(monkeypatch):
    monkeypatch.setattr(settings, "workflow_synthesis_model_enabled", True)
    monkeypatch.setattr(settings, "workflow_replan_model_enabled", True)


def plan(*task_types, merge=False, confirmation=False):
    return {
        "tasks": [{"task_type": task_type, "objective": f"Analyze {task_type}", "dependencies": []} for task_type in task_types],
        "merge_readiness": merge, "requires_human_confirmation": confirmation,
        "acceptance_criteria": ["Ground conclusions in evidence."],
    }


def review(**overrides):
    return {"findings": [], "overall_confidence": 0.9, "human_review_required": False, "summary": "No additional semantic issue.", **overrides}


def synthesis(decision="analysis_only", task_id="pr_review"):
    return {
        "decision": decision,
        **{
            key: [{"text": f"Model-generated {key} explanation", "task_ids": [task_id]}]
            for key in ("what", "why", "tradeoffs", "open_questions", "next_actions")
        },
    }


def context():
    return {"repo_id": "repo-1", "has_pr": True, "pr_id": "pr-1", "pr_number": 86,
            "has_issue": True, "issue_id": "issue-1", "has_ci_run": True, "workflow_run_id": "ci-1"}


def spec():
    return WorkflowSpec(goal="Review this change", trigger_message="review", tasks=[WorkflowTask(
        id="pr_review", task_type="pr_review", agent_name="pr_review_agent", objective="Review PR",
        claim=WorkflowClaim(target_type="pull_request", target_id="pr-1"),
    )])


def result(**overrides):
    return WorkflowTaskResult(**{
        "task_id": "pr_review", "task_type": "pr_review", "agent_name": "pr_review_agent",
        "status": "success", "summary": "Reviewed change", "evidence_count": 2, "confidence": 0.8,
        "output": {"merge_recommendation": "needs review"}, **overrides,
    })


def observer_input(results=None, workflow=None):
    return {"spec": (workflow or spec()).model_dump(mode="json"),
            "task_results": [item.model_dump(mode="json") for item in (results if results is not None else [result()])]}


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["只审 PR，不用分析 CI，也不要判断合并就绪度", "看看这份变更有没有遗漏的权限校验"])
async def test_planner_model_selects_tasks_instead_of_keyword_router(message):
    llm = StubLLM(plan("pr_review"))
    actual = WorkflowSpec.model_validate(await PlannerAgent(llm).run({"message": message}, context()))
    assert actual.planning.mode == "llm"
    assert {task.task_type for task in actual.tasks} == {"repo_health", "pr_review"}
    assert actual.merge_readiness is False
    task = next(task for task in actual.tasks if task.task_type == "pr_review")
    assert task.claim.target_id == "pr-1"
    assert task.agent_name == "pr_review_agent"
    assert task.claim.write_intent is False
    assert llm.calls[0][0] == PLAN_PROMPT
    assert "output_schema" in llm.calls[0][1]
    assert "ci_debug" in {task["task_type"] for task in llm.calls[0][1]["task_catalog"]}


@pytest.mark.asyncio
async def test_planner_enforces_safety_and_merge_tasks_even_if_model_omits_them():
    actual = WorkflowSpec.model_validate(await PlannerAgent(StubLLM(plan("pr_review", merge=True))).run(
        {"message": "Check readiness"}, {**context(), "requires_safety": True},
    ))
    assert {task.task_type for task in actual.tasks} == {"repo_health", "pr_review", "ci_debug", "safety_review"}
    assert actual.requires_human_confirmation
    assert actual.merge_readiness
    assert all(task.claim.target_id in {"repo-1", "pr-1", "ci-1"} for task in actual.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unknown_type", "unavailable", "duplicate", "cycle", "missing_dependency", "forged_claim", "empty_objective"])
async def test_planner_rejects_unexecutable_or_privileged_model_plans(kind):
    response = plan("pr_review")
    ctx = context()
    if kind == "unknown_type":
        response["tasks"][0]["task_type"] = "run_shell"
    elif kind == "unavailable":
        ctx["has_pr"] = False
    elif kind == "duplicate":
        response["tasks"] *= 2
    elif kind == "cycle":
        response["tasks"][0]["dependencies"] = ["pr_review"]
    elif kind == "missing_dependency":
        response["tasks"][0]["dependencies"] = ["unregistered"]
    elif kind == "forged_claim":
        response["tasks"][0]["claim"] = {"target_id": "another-repository", "write_intent": True}
    else:
        response["tasks"][0]["objective"] = " "
    actual = WorkflowSpec.model_validate(await PlannerAgent(StubLLM(response)).run({"message": "Review PR"}, ctx))
    assert actual.planning.mode == "rules_fallback"
    assert actual.planning.reason in {"invalid_output", "invalid_plan"}
    assert all(task.claim.target_id != "another-repository" for task in actual.tasks)


@pytest.mark.asyncio
async def test_model_replan_adds_only_new_tasks_and_preserves_confirmed_targets():
    previous = spec()
    previous.max_parallel_tasks = 2
    response = plan("rag_search")
    response["tasks"][0]["dependencies"] = ["pr_review"]
    llm = StubLLM(response)
    observation = WorkflowObservation(findings=[WorkflowFinding(
        finding_type="semantic_follow_up", severity="warning", message="Need historical design rationale", recommendation="Search history",
    )])
    actual = WorkflowSpec.model_validate(await PlannerAgent(llm).replan(
        {**observer_input(workflow=previous), "observation": observation.model_dump(mode="json")},
        {**context(), "pr_id": "newer-pr"},
    ))
    assert actual.planning.mode == "llm"
    assert actual.workflow_id == previous.workflow_id
    assert actual.max_parallel_tasks == 2
    assert actual.tasks[0] == previous.tasks[0]
    assert actual.tasks[0].claim.target_id == "pr-1"
    assert [task.task_type for task in actual.tasks] == ["pr_review", "rag_search"]
    assert llm.calls[0][1]["phase"] == "replan"
    assert "pr_review" not in {task["task_type"] for task in llm.calls[0][1]["task_catalog"]}


@pytest.mark.asyncio
async def test_replan_does_not_accept_dependencies_on_failed_old_tasks():
    response = plan("rag_search")
    response["tasks"][0]["dependencies"] = ["pr_review"]
    actual = WorkflowSpec.model_validate(await PlannerAgent(StubLLM(response)).replan(
        {**observer_input([result(status="error", error="failed")]),
         "observation": WorkflowObservation().model_dump(mode="json")}, context(),
    ))
    assert actual.planning.reason == "invalid_plan"
    for task in actual.tasks[1:]:
        assert "pr_review" not in task.dependencies


@pytest.mark.asyncio
async def test_observer_keeps_hard_blockers_without_redundant_model_review():
    ci = result(task_id="ci_debug", task_type="ci_debug", agent_name="ci_debug_agent", output={"is_merge_blocking": True})
    response = review(findings=[{
        "finding_type": "semantic_conflict", "severity": "warning", "message": "The described change conflicts with the stated scope",
        "task_ids": ["pr_review"], "recommendation": "Compare implementation with acceptance criteria",
    }], overall_confidence=1.0)
    llm = StubLLM(response)
    actual = await ObserverAgent(llm).run(observer_input([result(), ci]), {})
    assert any(item["finding_type"] == "ci_merge_gate" and item["source"] == "hard_rule" for item in actual["findings"])
    assert not llm.calls
    assert actual["human_review_required"]
    assert actual["overall_confidence"] < 0.8
    assert actual["model_review"]["mode"] == "rules"


@pytest.mark.asyncio
async def test_observer_no_findings_from_model_cannot_clear_ci_or_confirmation_gate():
    workflow = spec()
    workflow.requires_human_confirmation = True
    ci = result(task_id="ci_debug", task_type="ci_debug", output={"is_merge_blocking": True})
    actual = await ObserverAgent(StubLLM(review())).run(observer_input([result(), ci], workflow), {})
    assert {"ci_merge_gate", "missing_safety_review"} <= {item["finding_type"] for item in actual["findings"]}
    assert actual["human_review_required"]


@pytest.mark.asyncio
async def test_observer_rejects_hallucinated_task_references():
    response = review(findings=[{"finding_type": "semantic_conflict", "severity": "warning", "message": "Mismatch",
                                "task_ids": ["invented"], "recommendation": "Verify", "evidence": [
                                    {"fact_id": "f1", "quote": "Reviewed change"},
                                    {"fact_id": "f2", "quote": "needs review"}]}])
    actual = await ObserverAgent(StubLLM(response)).run(observer_input(), {})
    assert actual["model_review"]["reason"] == "invalid_fact_reference"
    assert actual["human_review_required"]
    assert not actual["findings"]
    assert all("invented" not in item["task_ids"] for item in actual["findings"])


@pytest.mark.asyncio
async def test_merge_readiness_without_ci_and_missing_planned_tasks_are_hard_blockers():
    workflow = spec()
    workflow.merge_readiness = True
    actual = await ObserverAgent(StubLLM(review())).run(observer_input([], workflow), {})
    assert {"missing_ci_evidence", "missing_pr_evidence", "task_missing"} <= {item["finding_type"] for item in actual["findings"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running", "skipped", "error"])
async def test_incomplete_results_cannot_produce_a_proceed_decision(status):
    inputs = observer_input([result(status=status)])
    observed = await ObserverAgent(StubLLM(review())).run(inputs, {})
    actual = await SynthesisAgent(StubLLM(synthesis("hold"))).run({**inputs, "observation": observed}, {})
    assert actual["model_execution"]["mode"] == "llm"
    assert "可以继续推进" not in actual["answer"]


@pytest.mark.asyncio
async def test_synthesis_uses_model_output_and_keeps_auditable_original_report():
    llm = StubLLM(synthesis())
    actual = await SynthesisAgent(llm).run({**observer_input(), "observation": WorkflowObservation().model_dump(mode="json")}, {})
    assert actual["model_execution"]["mode"] == "llm"
    assert "Model-generated why explanation" in actual["answer"]
    assert "Agent 输出摘要" in actual["answer"]
    assert "pr_review" in actual["answer"]
    assert llm.calls[0][0] == SYNTHESIS_PROMPT


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gate_override", "unknown_task", "uncited_fact"])
async def test_synthesis_rejects_gate_override_and_invented_evidence(kind):
    observation = WorkflowObservation(human_review_required=True, findings=[WorkflowFinding(
        finding_type="ci_merge_gate", severity="blocker", message="CI failed", recommendation="Fix tests",
    )])
    response = synthesis("hold")
    if kind == "gate_override":
        response["decision"] = "analysis_only"
    elif kind == "unknown_task":
        response["why"][0]["task_ids"] = ["invented"]
    else:
        response["what"][0]["task_ids"] = []
    actual = await SynthesisAgent(StubLLM(response)).run({**observer_input(), "observation": observation.model_dump(mode="json")}, {})
    assert actual["model_execution"]["mode"] == "rules_fallback"
    assert "Model-generated" not in actual["answer"]
    assert "暂缓合并" in actual["answer"]
    assert "规则降级" in actual["answer"]


@pytest.mark.asyncio
async def test_full_orchestrator_calls_all_three_models_but_dispatches_with_program_code():
    planner = StubLLM(plan("pr_review"))
    observer = StubLLM(review())
    synthesizer = StubLLM(synthesis())
    ran = []

    async def runner(task, workflow, ctx):
        ran.append(task.id)
        return result(task_id=task.id, task_type=task.task_type, agent_name=task.agent_name)

    report = await WorkflowOrchestrator(
        {"pr_review": runner}, planner=PlannerAgent(planner), observer=ObserverAgent(observer),
        synthesizer=SynthesisAgent(synthesizer),
    ).run({"message": "Analyze this change"}, {**context(), "has_repo": False, "disable_replan": True})
    assert ran == ["pr_review"]
    assert len(planner.calls) == len(observer.calls) == len(synthesizer.calls) == 1
    assert report["spec"]["planning"]["mode"] == "llm"
    assert report["observation"]["model_review"]["mode"] == "llm"
    assert report["synthesis"]["mode"] == "llm"
    assert report["metrics"]["model_fallback_count"] == 0
    assert any(event.get("planning", {}).get("mode") == "llm" for event in report["agent_events"] if event["type"] == "workflow_spec")

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.api.routes.chat import _persist_engineering_workflow, _workflow_plan_answer
    from app.db.models import AgentWorkflowRun, Repository
    from app.db.session import Base

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        repo = Repository(owner="test", name="model-flow", full_name="test/model-flow")
        db.add(repo)
        db.flush()
        run_id = _persist_engineering_workflow(db, repo, None, report)
        db.commit()
        saved = db.get(AgentWorkflowRun, run_id)
        assert saved.spec_json["planning"]["mode"] == "llm"
        assert saved.observation_json["model_review"]["mode"] == "llm"
        assert saved.metrics["synthesis_mode"] == "llm"
        assert saved.status == "success"
    assert "规划来源：大模型" in _workflow_plan_answer(WorkflowSpec.model_validate(report["spec"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"not": "a plan"}, RuntimeError("sensitive provider error")])
async def test_provider_failure_or_bad_output_is_explicitly_degraded(response):
    actual = await PlannerAgent(StubLLM(response)).run({"message": "Review PR"}, context())
    assert actual["planning"]["mode"] == "rules_fallback"
    assert "sensitive provider error" not in json.dumps(actual)


@pytest.mark.asyncio
async def test_model_timeout_and_input_budget_are_bounded(monkeypatch):
    class SlowLLM(StubLLM):
        async def chat_json(self, prompt, payload):
            await asyncio.sleep(10)

    monkeypatch.setattr(settings, "workflow_model_timeout_seconds", 0.01)
    proposal, execution = await request_workflow_model(SlowLLM(), PLAN_PROMPT, {}, PlanProposal)
    assert proposal is None and execution.reason == "timeout"
    monkeypatch.setattr(settings, "workflow_model_max_input_chars", 10)
    llm = StubLLM()
    proposal, execution = await request_workflow_model(llm, PLAN_PROMPT, {"goal": "x" * 100}, PlanProposal)
    assert proposal is None and execution.reason == "input_budget_exceeded"
    assert not llm.calls


@pytest.mark.asyncio
async def test_no_key_uses_explicit_offline_fallback_for_all_three_agents(monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", None)
    planned = await PlannerAgent().run({"message": "Review PR"}, context())
    observed = await ObserverAgent().run(observer_input(), {})
    synthesized = await SynthesisAgent().run({**observer_input(), "observation": observed}, {})
    assert planned["planning"]["reason"] == "not_configured"
    assert observed["model_review"]["reason"] == "not_configured"
    assert observed["human_review_required"]
    assert synthesized["model_execution"]["reason"] == "not_configured"
    assert "规则降级" in synthesized["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "http_error", "bad_json", "truncated"])
async def test_real_llm_client_http_contract_without_network(monkeypatch, mode):
    monkeypatch.setattr(settings, "llm_api_key", "test-only")
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["response_format"] == {"type": "json_object"}
        assert "JSON" in body["messages"][0]["content"]
        if mode == "http_error":
            return httpx.Response(503, json={"error": "unavailable"})
        content = json.dumps(plan("pr_review")) if mode == "success" else ("not json" if mode == "bad_json" else '{"tasks":[')
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    actual = await PlannerAgent(LLMClient()).run({"message": "Review PR"}, context())
    assert len(requests) == 1
    assert actual["planning"]["mode"] == ("llm" if mode == "success" else "rules_fallback")


def test_model_payload_projects_large_outputs_and_excludes_secret_fields():
    payload = task_evidence([result(output={"api_key": "never send", "nested": {"access_token": "never send"}, "details": "x" * 9000})])
    serialized = json.dumps(payload)
    assert "never send" not in serialized
    assert "[truncated]" in serialized
    assert len(serialized) < 4000
