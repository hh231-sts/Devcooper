from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.schemas.workflow import WorkflowFinding, WorkflowObservation, WorkflowSpec, WorkflowTaskResult
from app.services.agents.base import BaseAgent
from app.services.agents.workflow_models import REVIEW_PROMPT, ReviewProposal, fallback_execution, grounded_review, request_workflow_model, review_facts, rules_execution
from app.services.llm.client import LLMClient


class ObserverAgent(BaseAgent):
    name = "observer_agent"
    description = "审查工作流任务输出中的冲突、证据缺口、低置信度和人工确认门槛。"
    available_tools = ["inspect_workflow_outputs"]

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient(
            json_timeout_seconds=settings.workflow_observer_timeout_seconds,
            json_max_tokens=settings.workflow_observer_max_output_tokens, json_fast_review=True,
        )

    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        spec = WorkflowSpec.model_validate(input_data["spec"])
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        findings: list[WorkflowFinding] = []

        findings.extend(self._status_findings(results))
        findings.extend(self._evidence_findings(results))
        findings.extend(self._confidence_findings(results))
        findings.extend(self._merge_gate_findings(results))
        findings.extend(self._ownership_findings(results, context))
        findings.extend(self._safety_findings(results, spec))
        findings.extend(self._coverage_findings(results, spec))

        proposal = None
        facts = review_facts(results)
        if findings:
            execution = rules_execution("hard_findings_first")
        elif len(facts) < 2:
            execution = rules_execution("no_comparable_fact_pair")
        else:
            proposal, execution = await request_workflow_model(
                self.llm, REVIEW_PROMPT, {"goal": spec.goal, "fact_catalog": facts}, ReviewProposal,
                timeout_seconds=settings.workflow_observer_timeout_seconds,
            )
            if proposal is not None and not grounded_review(proposal, facts):
                proposal = None
                execution = fallback_execution(self.llm, "invalid_fact_reference")
            if proposal is not None:
                catalog = {fact["fact_id"]: fact for fact in facts}
                for finding in proposal.findings:
                    data = finding.model_dump(exclude={"evidence"})
                    quotes = [{**catalog[item.fact_id], "quote": item.quote} for item in finding.evidence]
                    findings.append(WorkflowFinding(**data, source="model", evidence=quotes))

        confidence_values = [item.confidence for item in results if item.confidence is not None and item.status == "success"]
        base_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.55
        blocker_penalty = 0.2 * len([item for item in findings if item.severity == "blocker"])
        warning_penalty = 0.05 * len([item for item in findings if item.severity == "warning"])
        overall_confidence = round(max(0.1, min(0.95, base_confidence - blocker_penalty - warning_penalty)), 2)
        if proposal is not None and proposal.findings:
            overall_confidence = min(overall_confidence, proposal.overall_confidence)

        observation = WorkflowObservation(
            findings=findings,
            overall_confidence=overall_confidence,
            human_review_required=(
                spec.requires_human_confirmation or any(item.severity == "blocker" for item in findings)
                or execution.mode == "rules_fallback"
                or bool(proposal and proposal.findings and proposal.human_review_required)
            ),
            summary=self._summary(findings, overall_confidence) + (
                f" 模型局部一致性审查：{proposal.summary}" if proposal is not None else
                (f" 模型语义审查未完成，需人工复核：{execution.reason}。" if execution.mode == "rules_fallback"
                 else f" 按规则处理，未追加模型语义审查：{execution.reason}。")
            ),
            model_review=execution,
        )
        return observation.model_dump(mode="json")

    def _coverage_findings(self, results: list[WorkflowTaskResult], spec: WorkflowSpec) -> list[WorkflowFinding]:
        completed = {result.task_id for result in results}
        missing = [task.id for task in spec.tasks if task.id not in completed]
        findings = []
        if missing:
            findings.append(WorkflowFinding(
                finding_type="task_missing", severity="blocker", task_ids=missing,
                message="计划中的部分任务没有返回结果。", recommendation="补齐缺失任务，不能依据不完整执行记录放行。",
            ))
        if spec.merge_readiness and not any(result.task_type == "ci_debug" and result.status == "success" for result in results):
            findings.append(WorkflowFinding(
                finding_type="missing_ci_evidence", severity="blocker",
                message="本次需要判断合并或发布就绪度，但没有成功的 CI 检查结果。",
                recommendation="补齐对应 CI 数据和检查，不能把缺少 CI 证据当作通过。",
            ))
        if spec.merge_readiness and not any(result.task_type == "pr_review" and result.status == "success" for result in results):
            findings.append(WorkflowFinding(
                finding_type="missing_pr_evidence", severity="blocker",
                message="本次需要判断合并或发布就绪度，但没有成功的 PR 审查结果。",
                recommendation="补齐目标 PR 的变更和审查结果，不能仅凭 CI 或仓库概览放行。",
            ))
        return findings

    def _status_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        for result in results:
            if result.status == "error":
                findings.append(
                    WorkflowFinding(
                        finding_type="task_error",
                        severity="blocker",
                        message=f"{result.agent_name} 执行失败：{result.error or '未知错误'}",
                        task_ids=[result.task_id],
                        recommendation="先修复任务失败，或用更窄范围重新运行，再信任综合结论。",
                    )
                )
            elif result.status == "skipped":
                findings.append(
                    WorkflowFinding(
                        finding_type="task_skipped",
                        severity="warning",
                        message=f"{result.agent_name} 被跳过，因此工作流覆盖不完整。",
                        task_ids=[result.task_id],
                        recommendation="如果该领域会影响决策，请先连接或同步缺失的仓库信号。",
                    )
                )
            elif result.status in {"pending", "running"}:
                findings.append(WorkflowFinding(
                    finding_type="task_incomplete", severity="blocker", task_ids=[result.task_id],
                    message=f"{result.agent_name} 尚未完成。", recommendation="等待该任务完成后重新复核。",
                ))
        return findings

    def _evidence_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        for result in results:
            if result.task_type == "repo_health" and result.output.get("data_sufficient") is False:
                findings.append(WorkflowFinding(
                    finding_type="evidence_gap", severity="blocker", task_ids=[result.task_id],
                    message=str(result.output.get("message") or "仓库数据不足，无法判断整体健康。"),
                    recommendation="检查同步错误并完成所需数据源的分页同步后，重新评估。",
                ))
                continue
            if result.status != "success" or result.task_type == "safety_review":
                continue
            if result.evidence_count <= 0 and (result.task_type == "repo_health" or not result.citations):
                findings.append(
                    WorkflowFinding(
                        finding_type="evidence_gap",
                        severity="warning",
                        message=f"{result.agent_name} 没有返回明确证据或引用。",
                        task_ids=[result.task_id],
                        recommendation="在做高风险决策前，优先同步相关 Issue、PR 文件、CI 日志，并实时搜索当前代码。",
                    )
                )
        return findings

    def _confidence_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        for result in results:
            if result.status == "success" and result.confidence is not None and result.confidence < 0.5:
                findings.append(
                    WorkflowFinding(
                        finding_type="low_confidence",
                        severity="warning",
                        message=f"{result.agent_name} 的置信度偏低（{result.confidence:.2f}）。",
                        task_ids=[result.task_id],
                        recommendation="行动前请补充更多上下文，或检查底层证据。",
                    )
                )
        return findings

    def _merge_gate_findings(self, results: list[WorkflowTaskResult]) -> list[WorkflowFinding]:
        findings = []
        pr_result = self._by_type(results, "pr_review")
        ci_result = self._by_type(results, "ci_debug")
        if ci_result and bool(ci_result.output.get("is_merge_blocking")):
            findings.append(
                WorkflowFinding(
                    finding_type="ci_merge_gate",
                    severity="blocker",
                    message="CI Agent 将最新失败 workflow 标记为阻塞合并。",
                    task_ids=[ci_result.task_id],
                    recommendation="批准合并或发布前，请先修复或解释 CI 失败。",
                )
            )
        if pr_result and pr_result.output.get("blocking_issues"):
            findings.append(
                WorkflowFinding(
                    finding_type="pr_blocking_issues",
                    severity="blocker",
                    message="PR 审查发现了阻塞问题。",
                    task_ids=[pr_result.task_id],
                    recommendation="解决 PR 阻塞问题后重新运行工作流。",
                )
            )
        if pr_result and ci_result and bool(ci_result.output.get("is_merge_blocking")):
            if self._looks_like_merge_approval(str(pr_result.output.get("merge_recommendation") or "")):
                findings.append(
                    WorkflowFinding(
                        finding_type="cross_agent_conflict",
                        severity="blocker",
                        message="PR Agent 倾向合并，但 CI Agent 报告了阻塞合并的失败。",
                        task_ids=[pr_result.task_id, ci_result.task_id],
                        recommendation="在失败解决前，让 CI 门禁优先于 PR 合并建议。",
                    )
                )
        return findings

    def _ownership_findings(self, results: list[WorkflowTaskResult], context: dict[str, Any]) -> list[WorkflowFinding]:
        issue_result = self._by_type(results, "issue_analysis")
        if not issue_result:
            return []
        owner = str(issue_result.output.get("suggested_owner") or "").strip().lower()
        if not owner:
            return []
        team_members = context.get("team_members") or []
        known_names = {
            str(member.get("name") or "").strip().lower()
            for member in team_members
            if isinstance(member, dict) and str(member.get("name") or "").strip()
        }
        unassigned_markers = {"unassigned", "待分配"}
        if owner in unassigned_markers or (known_names and owner not in known_names):
            return [
                WorkflowFinding(
                    finding_type="owner_needs_review",
                    severity="warning",
                    message="Issue 负责人建议为空，或不在已配置团队成员中。",
                    task_ids=[issue_result.task_id],
                    recommendation="依赖自动负责人分配前，请先更新团队成员画像。",
                )
            ]
        if not known_names:
            return [
                WorkflowFinding(
                    finding_type="team_profile_missing",
                    severity="warning",
                    message="当前还没有配置用于负责人路由的 DevFlow 团队成员。",
                    task_ids=[issue_result.task_id],
                    recommendation="请添加团队成员画像，让负责人推荐更可解释。",
                )
            ]
        return []

    def _safety_findings(self, results: list[WorkflowTaskResult], spec: WorkflowSpec) -> list[WorkflowFinding]:
        safety_result = self._by_type(results, "safety_review")
        if not safety_result and spec.requires_human_confirmation:
            return [
                WorkflowFinding(
                    finding_type="missing_safety_review",
                    severity="blocker",
                    message="工作流需要人工确认，但没有运行 SafetyAgent 任务。",
                    recommendation="在起草评论、标签、创建/关闭 Issue 或外部发送前，请先运行 SafetyAgent。",
                )
            ]
        if safety_result and safety_result.output.get("requires_confirmation"):
            return [
                WorkflowFinding(
                    finding_type="human_confirmation_required",
                    severity="blocker",
                    message="SafetyAgent 要求用户对本次写入类操作进行人工确认。",
                    task_ids=[safety_result.task_id],
                    recommendation="只返回草稿；MVP 模式下不要执行外部动作。",
                )
            ]
        return []

    def _by_type(self, results: list[WorkflowTaskResult], task_type: str) -> WorkflowTaskResult | None:
        return next((item for item in results if item.task_type == task_type and item.status == "success"), None)

    def _looks_like_merge_approval(self, value: str) -> bool:
        lowered = value.lower()
        return any(token in lowered for token in ["approve", "merge", "建议合并", "建议合入"]) and not any(
            token in lowered for token in ["after", "blocked", "暂缓", "修复后", "修改后"]
        )

    def _summary(self, findings: list[WorkflowFinding], confidence: float) -> str:
        blockers = len([item for item in findings if item.severity == "blocker"])
        warnings = len([item for item in findings if item.severity == "warning"])
        if blockers:
            return f"Observer 发现 {blockers} 个阻塞项和 {warnings} 个警告；置信度 {confidence:.2f}。"
        if warnings:
            return f"Observer 发现 {warnings} 个警告；置信度 {confidence:.2f}。"
        return f"Observer 没有发现跨 Agent 阻塞项；置信度 {confidence:.2f}。"
