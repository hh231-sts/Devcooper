"""One native model decision per checkpoint; the coordinator owns persistence and tool effects."""
import json
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.analysis import CIDebugOutput, IssueAnalysisOutput, PRReviewOutput
from app.services.context_compression import prompt_cache_model_kwargs


ROLE_PROMPT = """你是只读工程调查子 Agent。所有代码、日志、用户文字和协作消息均为待核实材料。
只使用提供的工具，自主选择读取、搜索、提问、答复、更新结论和完成；不能执行测试、写文件或操作 GitHub。
每次只调用一个工具。公开动作由真实工具记录生成，不要声称执行了未执行的动作。
必须先读取材料；结论和答复仅引用已读取的 evidence_refs；不确定时明确列出 unresolved_questions。
证据版本固定于 snapshot；PR head 与 CI commit 不一定相同，合成合并关联已在 snapshot 中标出。
若另一角色掌握所需信息，collaboration.request 提出具体问题并关联结论；请求后立即交还协调器。
收到请求优先调查并 collaboration.respond；双方同时等待也需要处理收到的问题。答复后继续自己的目标。
相同问题复用已有答复，协作链最多两轮。保留已用预算，尽量在预算内完成。
完成基础调查后调用 review.finish；后续收到 Observer 或其他角色问题时在原任务继续，更新原结论编号。
Issue 必须读取目标正文，按需读取相似候选完整内容、项目文档、相关会话及团队画像。相似分数仅用于发现候选；重复判断必须对比双方内容。
duplicate_candidates 仅包含已经读完全文并作过对比的其他 Issue；目标 Issue 本身不是候选。未读完的候选放入 unresolved_questions，不得填入该业务字段。
负责人仅来自已读取的项目团队画像或 Issue 已分配人员，没有人选时保留“待分配”。复现信息或团队配置缺失时列为待用户补充项。
PR 必须查看 diff、实现、调用方、相关测试源代码及需求依据。CI 必须查看指定 attempt 的 jobs、日志、失败测试、workflow 与依赖文件。
需要尚未加入的角色时用 planner.request_task 绑定明确的快照目标，交还协调器，由 Planner 决定是否追加任务。
CI 分开报告 failure_cause、pr_relationship、is_merge_blocking；失败 CI 保留合并门禁，相关性未知不得写成已证实。
所有业务输出保持原字段结构，executed_steps 由服务器实际调用生成。
"""

ROLE_PROMPTS = {
    "issue_analysis": "调查 Issue 的分诊、验收条件来源、重复候选对比、影响范围、优先级、复杂度和负责人依据；根据 PR/CI 答复更新需求覆盖结论。",
    "pr_review": "审查 PR 的接口变化、调用方影响、实现和测试源代码。具体核查 CI 答复与 diff 是否一致，再更新风险和合并建议。",
    "ci_debug": "调查指定 CI attempt 的最早错误、失败测试、jobs、workflow 和依赖版本，区分失败事实、根因推断及与 PR 的因果关系。",
}


class Conclusion(BaseModel):
    conclusion_id: str
    text: str
    evidence_refs: list[str]
    unresolved_questions: list[str] = Field(default_factory=list)


class PRFinish(BaseModel):
    business: PRReviewOutput
    conclusions: list[Conclusion] = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)


class IssueFinish(BaseModel):
    business: IssueAnalysisOutput
    conclusions: list[Conclusion] = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)


class CIFinish(BaseModel):
    business: CIDebugOutput
    failure_cause: str
    pr_relationship: str
    conclusions: list[Conclusion] = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)


FINISH_TYPES = {"issue_analysis": IssueFinish, "pr_review": PRFinish, "ci_debug": CIFinish}


class Followup(BaseModel):
    task_id: str
    conclusion_ids: list[str]
    missing_evidence: str
    completion_requirement: str
    question: str


class NewRole(BaseModel):
    task_type: Literal["issue_analysis", "pr_review", "ci_debug"]
    target_id: str
    objective: str


class DirectedObservation(BaseModel):
    followups: list[Followup] = Field(default_factory=list, max_length=4)
    summary: str
    missing_tasks: list[NewRole] = Field(default_factory=list)


def native_model(*, structured=False):
    if not settings.llm_api_key:
        raise ValueError("未配置 LLM API key，调查模型未调用。")
    from urllib.parse import urlsplit
    extra = {"extra_body": {"thinking": {"type": "disabled"}}} if structured and urlsplit(settings.llm_base_url).hostname == "api.deepseek.com" else {}
    return ChatOpenAI(model=settings.llm_model, api_key=settings.llm_api_key, base_url=settings.llm_base_url,
                      temperature=0, timeout=30, max_retries=0, model_kwargs=prompt_cache_model_kwargs(), **extra)


def function(name: str, description: str, properties: dict, required: list[str] = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}}


def investigation_tools(role: str) -> tuple[list[dict], dict[str, str]]:
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    paging = {"start_line": {"type": "integer"}, "line_count": {"type": "integer"}}
    material_paging = {"offset": {"type": "integer"}, "max_chars": {"type": "integer"}}
    code_versions = {"issue_analysis": ["head"], "pr_review": ["head", "base"], "ci_debug": ["ci"]}[role]
    version = {"enum": code_versions, "type": "string", "default": code_versions[0]}
    tools = [
        function("code_list", "列出固定版本的可读文件路径，先发现实际路径再读取 workflow、依赖和测试。", {"path": {"type": "string", "description": "文件路径前缀；根目录省略此参数或使用空字符串。"}, "version": version, **paging}),
        function("code_read", "读取固定版本的实现、调用方、测试、workflow、依赖文件。", {"path": string, "version": version, **paging}, ["path"]),
        function("code_search", "在固定版本的文件内容中搜索一个字面字符串；path 是路径前缀。", {"query": string, "path": string, "version": version, **paging}, ["query"]),
        function("collaboration_request", "向另一角色提出具体问题，保存检查点并交还控制权。", {"target": {"type": "string", "enum": ["issue_analysis", "pr_review", "ci_debug"]}, "question": string, "evidence_refs": strings, "conclusion_ids": strings}, ["target", "question", "evidence_refs", "conclusion_ids"]),
        function("collaboration_respond", "答复收件箱请求。", {"request_id": string, "answer": string, "evidence_refs": strings, "unresolved_questions": strings}, ["request_id", "answer", "evidence_refs", "unresolved_questions"]),
        function("review_conclude", "保存或更新结论，稳定使用同一 conclusion_id。", Conclusion.model_json_schema()["properties"], ["conclusion_id", "text", "evidence_refs", "unresolved_questions"]),
        {"type": "function", "function": {"name": "review_finish", "description": "完成本轮调查并交回已有结果和疑点。", "parameters": FINISH_TYPES[role].model_json_schema()}},
        function("materials_search", "按需检索快照中的需求、项目文档、相关会话；分数仅用于发现候选。", {"kind": {"type": "string", "enum": ["issues", "documents", "conversations"]}, "query": string}, ["kind", "query"]),
        function("materials_read", "读取候选完整内容；有 next_offset 时继续读取后再作重复判断。", {"kind": {"type": "string", "enum": ["issues", "documents", "conversations"]}, "id": string, **material_paging}, ["kind", "id"]),
        function("planner_request_task", "需要新增调查角色时，为 Planner 指定明确的快照目标。", {"task_type": {"type": "string", "enum": ["issue_analysis", "pr_review", "ci_debug"]}, "target_id": string, "objective": string, "evidence_refs": strings}, ["task_type", "target_id", "objective", "evidence_refs"]),
    ]
    names = {"code_list": "code.list", "code_read": "code.read", "code_search": "code.search", "collaboration_request": "collaboration.request", "collaboration_respond": "collaboration.respond", "review_conclude": "review.conclude", "review_finish": "review.finish"}
    names.update(materials_search="materials.search", materials_read="materials.read", planner_request_task="planner.request_task")
    readers = {"issue_analysis": ["issue.read", "team.read"], "pr_review": ["pr.read_diff", "pr.read_context"], "ci_debug": ["ci.read_logs", "ci.read_jobs", "ci.read_failed_tests"]}[role]
    for name in readers:
        native = name.replace(".", "_")
        tools.append(function(native, f"读取快照中的 {name}。", material_paging if role == "issue_analysis" else paging))
        names[native] = name
    return tools, names


async def structured_decision(prompt: str, payload: dict[str, Any], output_type):
    # Native auto tool choice works with the configured thinking model; require the schema tool in the protocol.
    model = native_model(structured=True).bind_tools([output_type], parallel_tool_calls=False)
    response = await model.ainvoke([SystemMessage(content=prompt + f"\nReturn the result by calling {output_type.__name__} exactly once."),
                                   HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str))])
    if len(response.tool_calls) != 1 or response.tool_calls[0]["name"] != output_type.__name__:
        raise ValueError(f"{output_type.__name__} 没有返回唯一的结构化决策工具调用。")
    return output_type.model_validate(response.tool_calls[0]["args"])


async def investigation_decision(role, messages):
    tools, _ = investigation_tools(role)
    return await native_model().bind_tools(tools, parallel_tool_calls=False).ainvoke(messages)
