from datetime import datetime, timedelta, timezone

from app.db.models import Issue, PullRequest, Repository, WorkflowRun


def repository_health_snapshot(repo: Repository, issues: list[Issue], prs: list[PullRequest], runs: list[WorkflowRun]) -> dict:
    """Describe local evidence and its coverage, without treating absence as health."""
    evidence_count = len(issues) + len(prs) + len(runs)
    metadata = repo.sync_metadata or {}
    sources = metadata.get("sources") or {}
    if not metadata:
        state = "unknown" if evidence_count or repo.last_sync_at else "not_synced"
    elif metadata.get("status") == "failed":
        state = "failed"
    elif any(not sources.get(key, {}).get("complete") for key in ("issues", "pull_requests", "workflow_runs")):
        state = "partial"
    else:
        attempted_at = datetime.fromisoformat(metadata["attempted_at"]) if metadata.get("attempted_at") else None
        if attempted_at and attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)
        if not attempted_at or datetime.now(timezone.utc) - attempted_at > timedelta(hours=24):
            state = "stale"
        else:
            state = "empty" if metadata.get("status") == "empty" or not evidence_count else "ready"
    messages = {
        "not_synced": "尚未同步仓库数据，暂无法评估仓库健康；请先执行同步。",
        "unknown": "已有本地记录，但缺少可验证的同步状态和覆盖范围；请重新同步后再评估。",
        "failed": "最近一次同步失败；已有记录仅可作为历史参考，暂无法评估当前仓库健康。",
        "partial": "同步数据不完整：部分数据源失败、未同步或达到数量上限；不能据此判断整个仓库健康。",
        "stale": "同步状态缺少有效时间或已超过 24 小时；数据可能过期，请刷新后再评估。",
        "empty": "本次同步请求成功，但未返回 Issue、PR、CI 记录；数据不足，暂无法评估当前仓库健康。",
        "ready": "以下为本地已同步记录的统计，不代表仓库整体健康或实时远端状态。",
    }
    return {
        "data_status": state,
        "data_sufficient": state == "ready",
        "status": "error" if state == "failed" else ("skipped" if state == "empty" or not evidence_count else "success"),
        "message": messages[state],
        "evidence_count": evidence_count,
        "confidence": None,  # No calibrated probability model exists for these counts.
        "sources": sources,
        "sync_errors": metadata.get("errors") or [],
        "last_sync_at": repo.last_sync_at.isoformat() if repo.last_sync_at else None,
    }
