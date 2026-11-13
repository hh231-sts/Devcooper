import asyncio
import logging

from app.core.config import settings
from app.db.concurrency import run_blocking
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

logger = logging.getLogger(__name__)


def _repository_ids():
    with SessionLocal() as db:
        return [row.id for row in db.query(Repository).filter(Repository.provider.in_(["github", "github_compatible"])).all()]


async def periodic_repo_sync_loop() -> None:
    await asyncio.sleep(5)
    while settings.auto_sync_enabled:
        repo_ids = await run_blocking(_repository_ids)

        for repo_id in repo_ids:
            try:
                await sync_repository_by_id(repo_id, RepoSyncRequest(limit=settings.auto_sync_limit))
            except Exception as exc:
                logger.warning("Periodic repo sync failed for %s: %s", repo_id, exc)

        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))
