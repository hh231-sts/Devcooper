import os

import pytest

from app.core.config import settings
from app.schemas.workflow import WorkflowClaim, WorkflowFinding, WorkflowObservation, WorkflowSpec, WorkflowTask, WorkflowTaskResult
from app.services.agents.ci_debug_agent import CIDebugAgent
from app.services.agents.observer_agent import ObserverAgent
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.pr_review_agent import PRReviewAgent
from app.services.agents.synthesis_agent import SynthesisAgent
from app.services.agents.workflow_orchestrator import WorkflowOrchestrator
from app.services.llm.client import LLMClient


pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    os.environ.get("DEVFLOW_RUN_LIVE_LLM") != "1" or not settings.llm_api_key,
    reason="Requires explicit live-model opt-in and LLM_API_KEY; real requests incur provider charges.",
)]


def live_context():
    return {"repo_id": "live-fixture", "has_repo": False, "has_pr": True, "pr_id": "pr-86",
            "pr_number": 86, "has_ci_run": True, "has_failed_ci": True,
            "workflow_run_id": "run-9001", "workflow_run_number": 9001}


def live_spec():
    return WorkflowSpec(goal="检查授权变更", trigger_message="检查授权变更", tasks=[WorkflowTask(
        id="pr_review", task_type="pr_review", agent_name="pr_review_agent", objective="审查 PR #86",
        claim=WorkflowClaim(target_type="pull_request", target_id="pr-86"),
    )])


def live_result():
    return WorkflowTaskResult(
        task_id="pr_review", task_type="pr_review", agent_name="pr_review_agent", status="success",
        summary="代码变更允许匿名请求访问管理接口。", evidence_count=1, confidence=0.8,
        output={"implementation_behavior": {"anonymous_requests": "accepted"}, "file": "admin.py"},
        citations=[{"type": "pull_request", "id": "pr-86", "title": "Auth change"}],
    )


async def test_live_planner_respects_negation_instead_of_keyword_hits():
    actual = WorkflowSpec.model_validate(await PlannerAgent().run(
        {"message": "只检查 PR 的权限风险，不要检查 CI，不要判断能否合并，不查询历史资料。"}, live_context(),
    ))
    assert actual.planning.mode == "llm", actual.planning.reason
    assert {task.task_type for task in actual.tasks} == {"pr_review"}
    assert not actual.merge_readiness


async def test_live_planner_understands_a_request_without_router_keywords():
    actual = WorkflowSpec.model_validate(await PlannerAgent().run(
        {"message": "这份变更有没有把不该开放的入口暴露给匿名用户？请检查代码差异。"}, live_context(),
    ))
    assert actual.planning.mode == "llm", actual.planning.reason
    assert "pr_review" in {task.task_type for task in actual.tasks}
    assert next(task for task in actual.tasks if task.task_type == "pr_review").claim.target_id == "pr-86"


async def test_live_observer_detects_a_semantic_requirement_implementation_conflict():
    implementation = live_result()
    requirement = WorkflowTaskResult(
        task_id="issue_analysis", task_type="issue_analysis", agent_name="issue_analyst_agent", status="success",
        summary="验收要求：所有管理接口必须拒绝匿名请求。", evidence_count=1, confidence=0.8,
        output={"acceptance_requirement": {"anonymous_requests": "must_be_rejected"}},
        citations=[{"type": "issue", "id": "issue-142", "title": "Require admin authentication"}],
    )
    actual = await ObserverAgent().run({
        "spec": live_spec().model_dump(mode="json"),
        "task_results": [item.model_dump(mode="json") for item in [implementation, requirement]],
    }, {})
    assert actual["model_review"]["mode"] == "llm", actual["model_review"]["reason"]
    assert any(item["source"] == "model" and item["finding_type"] in {"semantic_conflict", "unsupported_conclusion"} for item in actual["findings"])


async def test_rule_first_replan_keeps_old_claims_in_live_configuration():
    previous = live_spec()
    observation = WorkflowObservation(findings=[WorkflowFinding(
        finding_type="missing_context", severity="warning", task_ids=["pr_review"],
        message="缺少此前授权方案的设计记录，当前结论无法说明此次变更是否符合历史约束。",
        recommendation="检索历史设计记录和相关讨论以补齐证据。",
    )])
    actual = WorkflowSpec.model_validate(await PlannerAgent().replan({
        "spec": previous.model_dump(mode="json"), "task_results": [live_result().model_dump(mode="json")],
        "observation": observation.model_dump(mode="json"),
    }, live_context()))
    assert actual.planning.mode == "rules", actual.planning.reason
    assert actual.tasks[0] == previous.tasks[0]
    assert "rag_search" in {task.task_type for task in actual.tasks}
    assert len(actual.tasks) == len({task.id for task in actual.tasks})


async def test_live_end_to_end_planner_workers_observer_and_synthesis():
    class RecordingLLM(LLMClient):
        def __init__(self):
            super().__init__()
            self.nonempty_responses = []

        async def chat_json(self, prompt, payload):
            output = await super().chat_json(prompt, payload)
            self.nonempty_responses.append(bool(output))
            return output

    worker_llm = RecordingLLM()

    async def run_pr(task, workflow, ctx):
        output = await PRReviewAgent(worker_llm).run({
            "number": 86, "title": "Require authentication on admin API", "state": "open", "body": "Fix admin authorization",
            "files": [{"filename": "backend/admin.py", "additions": 1, "deletions": 1,
                       "patch": "-if user.is_admin:\n+if user is not None:\n     return admin_data"}],
            "comments": [],
        }, {"ci_summary": {"failed_runs": 1}})
        return WorkflowTaskResult(task_id=task.id, task_type=task.task_type, agent_name=task.agent_name,
                                  status="success", summary=output["summary"], output=output, evidence_count=1,
                                  confidence=output["confidence"], citations=[{"type": "pull_request", "id": "pr-86"}])

    async def run_ci(task, workflow, ctx):
        output = await CIDebugAgent(worker_llm).run({
            "name": "backend-tests", "status": "completed", "conclusion": "failure",
            "jobs": [{"name": "pytest", "conclusion": "failure"}],
            "logs_text": "FAILED tests/test_admin.py::test_non_admin_denied - AssertionError: expected 403, got 200. Required PR check failed.",
        }, {})
        return WorkflowTaskResult(task_id=task.id, task_type=task.task_type, agent_name=task.agent_name,
                                  status="success", summary="管理接口权限测试失败", output=output, evidence_count=1,
                                  confidence=output["confidence"], citations=[{"type": "workflow_run", "id": "run-9001"}])

    report = await WorkflowOrchestrator({"pr_review": run_pr, "ci_debug": run_ci}).run(
        {"message": "请结合 PR 和 CI 判断此次变更能否合并，只执行这两项检查，不检索历史，不执行任何对外写入。"},
        {**live_context(), "disable_replan": True},
    )
    assert report["spec"]["planning"]["mode"] == "llm", report["spec"]["planning"]
    assert report["observation"]["model_review"]["mode"] == "rules", report["observation"]["model_review"]
    assert report["synthesis"]["mode"] == "rules", report["synthesis"]
    assert worker_llm.nonempty_responses == [True, True]
    assert report["metrics"]["model_fallback_count"] == 0
    assert report["metrics"]["success_count"] == 2
    assert any(item["finding_type"] == "ci_merge_gate" and item["severity"] == "blocker" for item in report["observation"]["findings"])
    assert "规则处理" in report["final_answer"]
    assert "暂缓合并" in report["final_answer"]
