"use client";

import { useEffect, useState } from "react";

import { Card, GhostButton, PageTitle, PrimaryButton } from "@/components/ui/card";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";
import { WorkflowRunCard } from "@/components/workflow-run-card";

type PullRequest = {
  id: string;
  number: number;
  title: string;
  state: string;
  files: { filename: string; additions: number; deletions: number }[];
};

export default function PullRequestsPage() {
  const [prs, setPrs] = useState<PullRequest[]>([]);
  const [selected, setSelected] = useState<PullRequest | null>(null);
  const [runId, setRunId] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const selectedId = selected?.id;

  useEffect(() => {
    apiGet<Repository[]>("/api/repos").then((repos) => {
      if (repos[0]) {
        apiGet<PullRequest[]>(`/api/repos/${repos[0].id}/pull-requests`).then((items) => {
          setPrs(items);
          setSelected(items[0] ?? null);
        });
      }
    });
  }, []);

  useEffect(() => {
    setRunId("");
    if (!selectedId) return;
    let disposed = false;
    apiGet<{ workflow_run_id: string | null }>(`/api/pull-requests/${selectedId}/analysis-run`).then((data) => {
      if (!disposed) setRunId(data.workflow_run_id ?? "");
    }).catch((reason: Error) => { if (!disposed) setError(reason.message); });
    return () => { disposed = true; };
  }, [selectedId]);

  async function analyze() {
    if (!selected) return;
    setSubmitting(true); setError("");
    try {
      const data = await apiPost<{ workflow_run_id: string }>(`/api/pull-requests/${selected.id}/analyze`, {});
      setRunId(data.workflow_run_id);
    } catch (reason) { setError((reason as Error).message); }
    finally { setSubmitting(false); }
  }

  return (
    <>
      <PageTitle title="PR Review 助手" description="读取 changed files 和 diff，生成 PR 摘要、风险点、Review Checklist 与测试建议。" />
      <div className="grid gap-4 lg:grid-cols-[380px_1fr]">
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-ink">Pull Requests</h2>
            <span className="rounded-full bg-panel px-2.5 py-1 text-xs text-slate-500">{prs.length} 个</span>
          </div>
          <div className="mt-4 space-y-2">
            {prs.map((pr) => (
              <GhostButton key={pr.id} onClick={() => setSelected(pr)} className={selected?.id === pr.id ? "border-teal-300 bg-teal-50 text-teal-900" : ""}>
                #{pr.number} {pr.title}
              </GhostButton>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="font-semibold text-ink">Review 报告</h2>
          {selected && (
            <div className="mt-3 rounded-md bg-panel p-3">
              <div className="text-sm font-medium text-ink">#{selected.number} {selected.title}</div>
              <div className="mt-3 grid gap-2">
                {selected.files.map((file) => (
                  <div key={file.filename} className="flex items-center justify-between rounded-md bg-white px-3 py-2 text-sm text-slate-600">
                    <span className="truncate">{file.filename}</span>
                    <span className="ml-3 whitespace-nowrap text-xs text-slate-500">+{file.additions} / -{file.deletions}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <PrimaryButton onClick={analyze} disabled={!selected || submitting} className="mt-4">生成 PR 分析</PrimaryButton>
          {error && <p className="mt-3 text-sm text-rose-700">{error}</p>}
          {runId && <WorkflowRunCard key={runId} id={runId} />}
        </Card>
      </div>
    </>
  );
}
