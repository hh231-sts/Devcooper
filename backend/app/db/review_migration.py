"""Incremental PR/CI review migration; existing completed workflows remain untouched."""
from sqlalchemy import inspect, text


def upgrade_review_schema(engine) -> None:
    additions = {
        "pull_requests": {"base_sha": "VARCHAR(120)", "head_sha": "VARCHAR(120)", "merge_commit_sha": "VARCHAR(120)"},
        "workflow_runs": {
            "commit_sha": "VARCHAR(120)", "pull_requests_json": "JSON DEFAULT '[]'",
            "run_attempt": "INTEGER", "workflow_path": "TEXT",
        },
        "agent_workflow_runs": {
            "snapshot_id": "UUID REFERENCES review_snapshots(id)",
            "assistant_message_id": "UUID REFERENCES chat_messages(id)",
            "checkpoint_json": "JSON DEFAULT '{}'", "active_seconds": "FLOAT DEFAULT 0",
            "lease_owner": "VARCHAR(120)", "lease_expires_at": "TIMESTAMP WITH TIME ZONE",
            "cancel_requested": "BOOLEAN DEFAULT FALSE",
        },
        "agent_task_runs": {
            "checkpoint_json": "JSON DEFAULT '{}'", "budget_json": "JSON DEFAULT '{}'",
            "result_version": "INTEGER DEFAULT 0", "lease_owner": "VARCHAR(120)",
            "lease_expires_at": "TIMESTAMP WITH TIME ZONE",
        },
        "analysis_results": {"workflow_id": "UUID REFERENCES agent_workflow_runs(id)", "task_id": "VARCHAR(120)", "result_version": "INTEGER"},
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
