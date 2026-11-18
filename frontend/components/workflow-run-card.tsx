"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { API_BASE_URL, apiGet, apiPost } from "@/lib/api";

type ReviewEvent = { id: number; kind: string; task_id?: string; payload: Record<string, unknown>; created_at: string };
type ReviewTask = { task_id: string; agent_name: string; status: string; code_sha?: string; error?: string; result_version: number; result?: { output?: Record<string, unknown> }; budget: { model_calls?: number; tool_calls?: number; active_seconds?: number } };
type ReviewRun = {
  id: string; goal: string; status: string; phase?: string; cancel_requested: boolean; final_answer: string;
  active_seconds: number; tasks: ReviewTask[]; events: ReviewEvent[];
  snapshot: { id?: string; issue?: { number?: number; content_digest?: string; updated_at?: string }; code_sha?: string; pr?: { head_sha?: string; base_sha?: string }; ci?: { commit_sha?: string; run_attempt?: number; pr_relationship?: string }; ci_missing?: string };
  evidence: Array<{ id: string; task_id: string; version: string; location: string; excerpt: string }>;
  conclusions: Array<{ task_id: string; conclusion_id: string; version: number; text: string; evidence_refs: string[]; unresolved_questions: string[] }>;
};

const labels: Record<string, string> = {
  queued: "已排队", running: "执行中", waiting: "等待协作", pending: "待执行", success: "已完成", completed: "已完成",
  partial: "部分完成", failed: "失败", error: "失败", skipped: "未执行", cancelled: "已取消",
  submitted: "已提交", snapshot_created: "已固定审查版本", planned: "任务规划", call_started: "开始调用",
  model_decision: "模型决策", evidence_read: "读取证据", tool_completed: "工具返回", conclusion_updated: "结论更新",
  collaboration_requested: "提出问题", collaboration_responded: "答复问题", observer_result: "Observer 审查",
  observer_requested: "Observer 定向补查", task_result: "任务结果", task_ended: "任务结束",
  followup_unavailable: "补查无法继续", observer_limit_reached: "达到补查轮数上限", cancel_requested: "请求取消",
  observer_failed: "Observer 调用失败", task_requested: "请求新增角色", planner_responded: "Planner 答复", followup_reused: "复用补查答复"
};
const active = (status: string) => status === "queued" || status === "running";

export function WorkflowRunCard({ id, onReport }: { id: string; onReport?: (answer: string) => void }) {
  const [run, setRun] = useState<ReviewRun | null>(null);
  const [events, setEvents] = useState<ReviewEvent[]>([]);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const callback = useRef(onReport);
  callback.current = onReport;

  useEffect(() => {
    let source: EventSource | undefined;
    let disposed = false;
    setRun(null);
    setEvents([]);
    setError("");
    setConnected(false);
    async function refresh() {
      const detail = await apiGet<ReviewRun>(`/api/chat/workflow-runs/${id}`);
      if (disposed) return detail;
      setRun(detail);
      setEvents((current) => [...new Map([...current, ...detail.events].map((item) => [item.id, item])).values()].sort((a, b) => a.id - b.id));
      if (!active(detail.status) && detail.final_answer) callback.current?.(detail.final_answer);
      return detail;
    }
    refresh().then((detail) => {
      if (disposed || !active(detail.status)) return;
      const after = detail.events.at(-1)?.id ?? 0;
      source = new EventSource(`${API_BASE_URL}/api/chat/workflow-runs/${id}/events?after=${after}`);
      source.onopen = () => { setConnected(true); setError(""); };
      source.onerror = () => { setConnected(false); setError("进度连接已断开，正在按事件编号恢复；任务继续在后台执行。"); };
      source.addEventListener("review_event", (message) => {
        const record = JSON.parse((message as MessageEvent).data) as ReviewEvent;
        setEvents((current) => current.some((item) => item.id === record.id) ? current : [...current, record]);
        if (["task_result", "task_ended", "conclusion_updated", "snapshot_created", "completed"].includes(record.kind)) {
          void refresh().catch((reason: Error) => setError(reason.message));
        }
      });
      source.addEventListener("settled", () => {
        source?.close(); setConnected(false);
        void refresh().catch((reason: Error) => setError(reason.message));
      });
    }).catch((reason: Error) => setError(reason.message));
    return () => { disposed = true; source?.close(); };
  }, [id]);

  async function cancel() {
    try {
      await apiPost(`/api/chat/workflow-runs/${id}/cancel`, {});
      setRun((current) => current ? { ...current, cancel_requested: true } : current);
    } catch (reason) { setError((reason as Error).message); }
  }

  return (
    <section className="my-3 min-w-0 rounded-lg border border-teal-200 bg-teal-50/40 p-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <strong>工程审查 · {run ? labels[run.status] || run.status : "正在读取"}</strong>
        {run && active(run.status) && <button className="rounded border border-line bg-white px-2 py-1 text-xs" disabled={run.cancel_requested} onClick={cancel}>{run.cancel_requested ? "正在取消" : "取消运行"}</button>}
      </div>
      <p className="mt-1 break-all text-xs text-slate-500">运行 {id}{run && active(run.status) ? ` · ${connected ? "进度已连接" : "后台执行"}` : ""}</p>
      {error && <p className="mt-2 text-xs text-rose-700">{error}</p>}
      {run && <>
        <p className="mt-3 font-medium">{run.goal}</p>
        <div className="mt-2 space-y-1 break-all text-xs text-slate-600">
          {run.snapshot.issue && <p>Issue #{run.snapshot.issue.number} · 更新于 {run.snapshot.issue.updated_at || "未提供"} · 摘要 {run.snapshot.issue.content_digest}</p>}
          {run.snapshot.code_sha && <p>提交时仓库 HEAD {run.snapshot.code_sha}</p>}
          {run.snapshot.pr?.head_sha && <p>PR base {run.snapshot.pr.base_sha} → head {run.snapshot.pr.head_sha}</p>}
          {run.snapshot.ci?.commit_sha && <p>CI commit {run.snapshot.ci.commit_sha} · attempt {run.snapshot.ci.run_attempt} · {run.snapshot.ci.pr_relationship}</p>}
          {run.snapshot.ci_missing && <p className="text-amber-800">{run.snapshot.ci_missing}</p>}
          <p>累计活跃时间 {run.active_seconds.toFixed(1)}/300 秒</p>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {run.tasks.map((task) => <div key={task.task_id} className="rounded border border-line bg-white p-2">
            <p className="font-medium">{task.agent_name} · {labels[task.status] || task.status}</p>
            {task.code_sha && <p className="break-all text-xs text-slate-500">调查代码 SHA {task.code_sha}</p>}
            <p className="text-xs text-slate-500">结果 v{task.result_version} · 模型 {task.budget.model_calls ?? 0}/8 · 工具 {task.budget.tool_calls ?? 0}/16 · {(task.budget.active_seconds ?? 0).toFixed(1)}/180 秒</p>
            {task.error && <p className="mt-1 text-xs text-rose-700">{task.error}</p>}
            {task.result?.output?.business_result_precedes_conclusions === true && <p className="mt-1 text-xs text-amber-800">该业务结果早于补查结论，当前判断见最新结论。</p>}
            {task.result?.output && <details className="mt-2 text-xs"><summary className="cursor-pointer text-teal-700">业务结果、分诊与草稿</summary><pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap">{JSON.stringify(task.result.output, null, 2)}</pre></details>}
          </div>)}
        </div>
        <details className="mt-3" open>
          <summary className="cursor-pointer font-medium">协作时间线 · {events.length} 条</summary>
          <ol className="mt-2 max-h-80 space-y-2 overflow-auto border-l border-teal-200 pl-3">
            {events.map((record) => <li key={record.id} className="text-xs">
              <p className="font-medium">#{record.id} {labels[record.kind] || record.kind}{record.task_id ? ` · ${record.task_id}` : ""}</p>
              <time className="text-slate-400">{new Date(record.created_at).toLocaleString("zh-CN")}</time>
              <details><summary className="cursor-pointer text-teal-700">问题、答复与执行详情</summary><pre className="mt-1 whitespace-pre-wrap break-words rounded bg-white p-2">{JSON.stringify(record.payload, null, 2)}</pre></details>
            </li>)}
          </ol>
        </details>
        {run.conclusions.length > 0 && <details className="mt-3"><summary className="cursor-pointer font-medium">最新结论</summary>{run.conclusions.map((item) => <div key={`${item.task_id}-${item.conclusion_id}`} className="mt-2 rounded bg-white p-2"><p>{item.task_id} / {item.conclusion_id} v{item.version}: {item.text}</p><p className="break-all text-xs text-slate-500">证据：{item.evidence_refs.join(", ") || "未提供"}</p>{item.unresolved_questions.map((question, index) => <p key={index} className="text-xs text-amber-800">待查：{question}</p>)}</div>)}</details>}
        {run.evidence.length > 0 && <details className="mt-3"><summary className="cursor-pointer font-medium">证据位置与读取片段</summary>{run.evidence.map((item) => <details key={item.id} className="mt-2 rounded bg-white p-2"><summary className="cursor-pointer break-all text-xs">{item.location} · {item.version} · {item.id}</summary><pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{item.excerpt}</pre></details>)}</details>}
        {run.final_answer && <div className="mt-4 border-t border-teal-200 pt-3"><ReactMarkdown remarkPlugins={[remarkGfm]}>{run.final_answer}</ReactMarkdown></div>}
      </>}
    </section>
  );
}
