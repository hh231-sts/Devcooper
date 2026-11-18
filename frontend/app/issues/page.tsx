"use client";

import { useEffect, useState } from "react";

import { Card, GhostButton, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";
import { WorkflowRunCard } from "@/components/workflow-run-card";

type Issue = { id: string; number: number; title: string; state: string; labels: string[]; body?: string };

export default function IssuesPage() {
  const [issues, setIssues] = useState<Issue[]>([]);
  const [selected, setSelected] = useState<Issue | null>(null);
  const [runId, setRunId] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const selectedIssueId = selected?.id;

  useEffect(() => {
    apiGet<Repository[]>("/api/repos").then((repos) => {
      if (repos[0]) {
        apiGet<Issue[]>(`/api/repos/${repos[0].id}/issues`).then((items) => {
          setIssues(items);
          setSelected(items[0] ?? null);
        });
      }
    });
  }, []);

  useEffect(() => {
    setRunId("");
    if (!selectedIssueId) return;
    let disposed = false;
    apiGet<{ workflow_run_id: string | null }>(`/api/issues/${selectedIssueId}/analysis-run`).then((data) => {
      if (!disposed) setRunId(data.workflow_run_id ?? "");
    }).catch((reason: Error) => { if (!disposed) setError(reason.message); });
    return () => { disposed = true; };
  }, [selectedIssueId]);

  async function analyze() {
    if (!selected) return;
    setSubmitting(true);
    setError("");
    try {
      const data = await apiPost<{ workflow_run_id: string }>(`/api/issues/${selected.id}/analyze`, {});
      setRunId(data.workflow_run_id);
    } catch (reason) { setError((reason as Error).message); }
    finally { setSubmitting(false); }
  }

  return (
    <>
      <PageTitle title="Issue 智能分析" description="自动生成 Issue 分类、优先级、复杂度、负责人建议、相似问题和下一步行动项。" />
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-ink">Issue 列表</h2>
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{issues.length} 条</span>
          </div>
          <div className="mt-4 space-y-2">
            {issues.map((issue) => (
              <GhostButton key={issue.id} onClick={() => setSelected(issue)} className={selected?.id === issue.id ? "border-teal-300 bg-teal-50 text-teal-900" : ""}>
                <div className="flex items-center justify-between gap-3">
                  <span>#{issue.number} {issue.title}</span>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs">{issue.state}</span>
                </div>
              </GhostButton>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-ink">分析结果</h2>
          {selected && (
            <div className="mt-3 rounded-md bg-panel p-3 text-sm leading-6 text-slate-600">
              <div className="font-medium text-ink">#{selected.number} {selected.title}</div>
              <div className="mt-1">{selected.body || "暂无正文"}</div>
            </div>
          )}
          <PrimaryButton onClick={analyze} disabled={!selected || submitting} className="mt-4">{submitting ? "正在提交" : runId ? "重新分析" : "分析当前 Issue"}</PrimaryButton>
          {error && <p className="mt-3 text-sm text-rose-700">{error}</p>}
          {runId && <WorkflowRunCard key={runId} id={runId} />}
        </Card>
      </div>
    </>
  );
}
