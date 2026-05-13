from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from celery import Celery

from api.config import settings

logger = logging.getLogger(__name__)


async def _run_pipeline_in_loop(project_id: UUID) -> None:
    """Run the LangGraph pipeline, then dispose the async DB pool.

    Celery invokes ``asyncio.run()`` once per task (new event loop each time). A process-global
    :class:`AsyncEngine` must not reuse connections across loops — that causes
    ``attached to a different loop`` and asyncpg ``another operation is in progress``.
    """
    from core.ai.orchestrator import run_pipeline
    from db.session import engine as aio_engine

    try:
        await run_pipeline(project_id)
    finally:
        await aio_engine.dispose()


celery_app = Celery(
    "deployforge",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=900,
    task_soft_time_limit=600,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=50,
)


@celery_app.task(bind=True, name="deployforge.run_pipeline", max_retries=2)
def run_pipeline_task(self, project_id: str) -> dict:
    """Execute the full LangGraph pipeline for a project."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from db.models import Project

    logger.info("Starting pipeline for project %s (attempt %d)", project_id, self.request.retries + 1)

    try:
        asyncio.run(_run_pipeline_in_loop(UUID(project_id)))
    except Exception as exc:
        logger.exception("Pipeline failed for project %s", project_id)

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)

        _mark_project_failed(project_id, str(exc))
        return {"project_id": project_id, "status": "failed", "error": str(exc)}

    engine = create_engine(settings.sync_database_url)
    try:
        with Session(engine) as session:
            proj = session.get(Project, UUID(project_id))
            db_status = proj.status if proj else "unknown"
    finally:
        engine.dispose()

    logger.info("Pipeline run finished for project %s (DB status=%s)", project_id, db_status)
    return {"project_id": project_id, "status": db_status}


def _mark_project_failed(project_id: str, error_message: str) -> None:
    """Update project status to 'failed' after all retries are exhausted."""
    from sqlalchemy import update
    from sqlalchemy.orm import Session
    from sqlalchemy import create_engine

    from db.models import Project

    logger.warning("Marking project %s as permanently failed: %s", project_id, error_message)

    engine = create_engine(settings.sync_database_url)
    with Session(engine) as session:
        session.execute(
            update(Project)
            .where(Project.id == UUID(project_id))
            .values(status="failed", error_summary=error_message[:2000])
        )
        session.commit()
    engine.dispose()
