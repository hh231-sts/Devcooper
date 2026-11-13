import copy
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Base,
    Document,
    Issue,
    KnowledgeBaseConfig,
    KnowledgeSourceDocument,
    PRFile,
    PRReviewComment,
    PullRequest,
    Repository,
    WorkflowRun,
)


@contextmanager
def evaluation_session(source: Session, repo_id: UUID):
    """Copy project inputs, never production history or evaluation outputs."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with Session(engine, autoflush=False) as sandbox:
            with source.get_bind().connect() as connection:
                if connection.dialect.name == "postgresql":
                    connection = connection.execution_options(isolation_level="REPEATABLE READ")
                with Session(bind=connection) as reader:
                    if connection.dialect.name == "postgresql":
                        reader.execute(text("SET TRANSACTION READ ONLY"))
                        reader.execute(text("SET LOCAL statement_timeout = '30s'"))
                    elif connection.dialect.name == "sqlite":
                        reader.execute(text("BEGIN"))
                    for model in (Repository, Issue, PullRequest, WorkflowRun, Document, KnowledgeBaseConfig, KnowledgeSourceDocument, PRFile, PRReviewComment):
                        table = model.__table__
                        if model is Repository:
                            predicate = table.c.id == repo_id
                        elif model in (PRFile, PRReviewComment):
                            predicate = table.c.pr_id.in_(select(PullRequest.id).where(PullRequest.repo_id == repo_id))
                        else:
                            predicate = table.c.repo_id == repo_id
                        rows = copy.deepcopy([dict(row) for row in reader.execute(select(table).where(predicate)).mappings()])
                        if rows:
                            sandbox.execute(table.insert(), rows)
            sandbox.commit()
            try:
                yield sandbox
            finally:
                sandbox.rollback()
    finally:
        engine.dispose()
