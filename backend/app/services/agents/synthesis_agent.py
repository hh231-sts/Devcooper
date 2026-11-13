from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.schemas.workflow import WorkflowObservation, WorkflowSpec, WorkflowTaskResult
from app.services.agents.base import BaseAgent
from app.services.agents.workflow_models import SYNTHESIS_PROMPT, SynthesisProposal, execution_label, fallback_execution, request_workflow_model, rules_execution, task_evidence
from app.services.llm.client import LLMClient


class SynthesisAgent(BaseAgent):
    name = "synthesis_agent"
    description = "将多 Agent 工作流综合成一份工程决策备忘录。"
    available_tools = ["synthesize_workflow_report"]

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        spec = WorkflowSpec.model_validate(input_data["spec"])
        results = [WorkflowTaskResult.model_validate(item) for item in input_data.get("task_results") or []]
        observation = WorkflowObservation.model_validate(input_data["observation"])
        answer = self._build_answer(spec, results, observation)
        decision = self._decision_code(observation, results)
        if not settings.workflow_synthesis_model_enabled:
            execution = rules_execution("rules_first_synthesis")
            answer += (
                f"\n\n处理来源：Planner={execution_label(spec.initial_planning or spec.planning)}；"
                f"Observer={execution_label(observation.model_review)}；Synthesis={execution_label(execution)}。"
            )
            return {"answer": answer, "model_execution": execution.model_dump(mode="json")}
        proposal, execution = await request_workflow_model(
            self.llm, SYNTHESIS_PROMPT,
            {
                "goal": spec.goal, "authoritative_decision": decision,
                "task_results": task_evidence(results), "observation": observation.model_dump(mode="json"),
            },
            SynthesisProposal,
        )
        if proposal is not None:
            known_ids = {result.task_id for result in results}
            statements = [item for section in (proposal.what, proposal.why, proposal.tradeoffs, proposal.open_questions, proposal.next_actions) for item in section]
            invalid_reference = any(set(item.task_ids) - known_ids for item in statements)
            uncited_fact = any(not item.task_ids for item in [*proposal.what, *proposal.why])
            if proposal.decision != decision or invalid_reference or uncited_fact:
                execution = fallback_execution(self.llm, "invalid_synthesis_or_gate_override")
            else:
                sections = [
                    ("What｜综合判断", proposal.what), ("Why｜证据与原因", proposal.why),
                    ("Tradeoffs｜取舍", proposal.tradeoffs), ("Open Questions｜待确认", proposal.open_questions),
                    ("Next Actions｜建议动作", proposal.next_actions),
                ]
                narrative = []
                for title, items in sections:
                    narrative.extend([f"### {title}", *[
                        f"- {item.text}" + (f" [任务：{', '.join(item.task_ids)}]" if item.task_ids else "")
                        for item in items
                    ], ""])
                answer += "\n\n## 模型辅助综合\n\n" + "\n".join(narrative).strip()
                if decision in {"hold", "human_review"}:
                    answer += "\n\n执行限制：上述模型分析不能解除阻塞项或代替人工确认，当前不得据此直接合并、发布或对外写入。"
        answer += (
            f"\n\n处理来源：Planner={execution_label(spec.initial_planning or spec.planning)}；"
            f"Observer={execution_label(observation.model_review)}；Synthesis={execution_label(execution)}。"
        )
        return {"answer": answer, "model_execution": execution.model_dump(mode="json")}

    def _decision_code(self, observation: WorkflowObservation, results: list[WorkflowTaskResult]) -> str:
        if any(finding.severity == "blocker" for finding in observation.findings) or any(result.status != "success" for result in results):
            return "hold"
        if observation.human_review_required:
            return "human_review"
        if observation.findings:
            return "caution"
        return "analysis_only"

    def _build_answer(self, spec: WorkflowSpec, results: list[WorkflowTaskResult], observation: WorkflowObservation) -> str:
        blockers = [item for item in observation.findings if item.severity == "blocker"]
        warnings = [item for item in observation.findings if item.severity == "warning"]
        decision = self._decision(blockers, warnings, results)
        if any(result.status != "success" for result in results) and not blockers:
            decision = "任务执行不完整，当前不建议直接推进。"
        if observation.human_review_required and not blockers and all(result.status == "success" for result in results):
            review_notice = "本次结论需要人工复核，不得据此直接执行外部操作。"
            decision = f"{decision} {review_notice}" if results and all(result.task_type == "repo_health" for result in results) else review_notice

        lines = [
            "## 多 Agent 协作结论",
            "",
            f"目标：{spec.goal}",
            f"总体判断：{decision}",
            f"Observer 置信度：{observation.overall_confidence:.2f}",
            "",
            "## 协作任务图",
        ]
        for task in spec.tasks:
            deps = f"，依赖：{', '.join(task.dependencies)}" if task.dependencies else ""
            lines.append(f"- {task.id}: {task.agent_name} 负责 {task.objective}{deps}")

        lines.extend(["", "## What｜Agent 输出摘要"])
        for result in results:
            lines.append(f"- {result.agent_name} [{result.status}]: {result.summary or '无摘要。'}")

        lines.extend(["", "## Observer 检查"])
        if observation.findings:
            for finding in observation.findings:
                lines.append(f"- {finding.severity.upper()}: {finding.message} 建议：{finding.recommendation}")
        else:
            lines.append("- 未发现跨 Agent 结论冲突。")

        lines.extend(["", "## Why｜决策依据与待确认项"])
        lines.extend(f"- {item}" for item in self._decision_basis(results, observation))
        lines.extend(["", "## Tradeoffs｜取舍"])
        if blockers or any(result.status != "success" for result in results):
            lines.append("- 当前阻塞或未完成任务优先于推进速度；先处理已知事项，不以模型建议替代门禁。")
        elif observation.human_review_required:
            lines.append("- 可以保留分析和草稿，但人工复核完成前不执行外部操作。")
        else:
            lines.append("- 本报告仅覆盖已给定的任务结果，不以局部检查代替范围外验证。")
        lines.extend(["", "## Open Questions｜待确认"])
        lines.extend(f"- {finding.message} {finding.recommendation}" for finding in observation.findings)
        if not observation.findings:
            lines.append("- 未发现新的明确矛盾不等于生产验收通过；最终合入和外部操作仍需按团队流程确认。")

        next_steps = self._next_steps(blockers, warnings, results)
        lines.extend(["", "## Next Actions｜下一步"])
        lines.extend(f"- {item}" for item in next_steps)
        return "\n".join(lines).strip()

    def _decision(
        self,
        blockers: list[Any],
        warnings: list[Any],
        results: list[WorkflowTaskResult],
    ) -> str:
        if blockers:
            if any(item.finding_type in {"ci_merge_gate", "cross_agent_conflict"} for item in blockers):
                return "暂缓合并或发布，先处理 CI/跨 Agent 冲突。"
            if any(item.finding_type == "human_confirmation_required" for item in blockers):
                return "只生成草稿，等待人工确认后再执行外部写操作。"
            return "存在阻塞项，当前不建议直接推进。"
        pr_result = self._by_type(results, "pr_review")
        if pr_result and pr_result.output.get("merge_recommendation"):
            return f"按 PR Agent 建议推进：{pr_result.output.get('merge_recommendation')}。"
        if warnings:
            return "可以继续推进，但需要补齐 Observer 标出的证据或团队信息。"
        if results and all(result.task_type == "repo_health" for result in results):
            return "已生成仓库数据概览；整体健康仍需结合具体 Issue、PR 和 CI 分析。"
        return "可以继续推进，未发现明显跨域阻塞。"

    def _next_steps(
        self,
        blockers: list[Any],
        warnings: list[Any],
        results: list[WorkflowTaskResult],
    ) -> list[str]:
        steps: list[str] = []
        ci_result = self._by_type(results, "ci_debug")
        pr_result = self._by_type(results, "pr_review")
        issue_result = self._by_type(results, "issue_analysis")

        if ci_result and ci_result.output.get("is_merge_blocking"):
            debug_steps = [str(item) for item in ci_result.output.get("debug_steps") or [] if str(item).strip()]
            steps.extend(debug_steps[:3] or ["先定位并修复最新失败 CI。"])
        if pr_result:
            blocking = [str(item) for item in pr_result.output.get("blocking_issues") or [] if str(item).strip()]
            tests = [str(item) for item in pr_result.output.get("test_suggestions") or [] if str(item).strip()]
            steps.extend(blocking[:2])
            steps.extend(tests[:2])
        if issue_result:
            actions = [str(item) for item in issue_result.output.get("checklist") or issue_result.output.get("action_items") or [] if str(item).strip()]
            steps.extend(actions[:2])
        for finding in [*blockers, *warnings]:
            if finding.recommendation:
                steps.append(str(finding.recommendation))
        if not steps:
            steps.append("把本次 workflow 结论回填到 PR/Issue 描述或团队同步中。")
        return list(dict.fromkeys(steps))[:6]

    def _decision_basis(self, results: list[WorkflowTaskResult], observation: WorkflowObservation) -> list[str]:
        basis: list[str] = []
        for result in results:
            confidence = f"，置信度 {result.confidence:.2f}" if result.confidence is not None else ""
            evidence = f"，证据 {result.evidence_count} 条" if result.evidence_count else ""
            basis.append(f"{result.agent_name}：{result.summary or '无摘要'}{evidence}{confidence}。")
        if observation.findings:
            for finding in observation.findings:
                basis.append(f"待确认：{finding.message} 处理建议：{finding.recommendation}")
        else:
            basis.append("待确认：最终合入、评论或外部发送仍需要人工按团队流程确认。")
        return basis[:8]

    def _by_type(self, results: list[WorkflowTaskResult], task_type: str) -> WorkflowTaskResult | None:
        return next((item for item in results if item.task_type == task_type and item.status == "success"), None)
