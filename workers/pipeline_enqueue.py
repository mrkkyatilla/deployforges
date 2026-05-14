"""Schedule the LangGraph pipeline: Celery (production) or in-process (local / fallback)."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException

from api.config import settings

logger = logging.getLogger(__name__)


async def _run_pipeline_on_api_loop(project_id: UUID) -> None:
    """Run pipeline in the API process event loop (do **not** dispose the shared async engine)."""
    from core.ai.orchestrator import run_pipeline

    await run_pipeline(project_id)


def schedule_pipeline(project_id: UUID, background_tasks: BackgroundTasks) -> None:
    """Queue ``run_pipeline`` via Celery and/or FastAPI background tasks based on settings."""
    mode = settings.pipeline_enqueue_mode

    if mode == "background":
        background_tasks.add_task(_run_pipeline_on_api_loop, project_id)
        logger.info(
            "Pipeline %s scheduled in-process (DF_PIPELINE_ENQUEUE_MODE=background)",
            project_id,
        )
        return

    if mode == "auto":
        try:
            from workers.build_worker import run_pipeline_task

            run_pipeline_task.delay(str(project_id))
            return
        except Exception as exc:
            logger.warning(
                "Celery enqueue failed for project %s (%s); running pipeline in-process (auto).",
                project_id,
                exc,
            )
            background_tasks.add_task(_run_pipeline_on_api_loop, project_id)
            return

    # strict celery
    try:
        from workers.build_worker import run_pipeline_task

        run_pipeline_task.delay(str(project_id))
    except Exception as exc:
        logger.exception("Failed to enqueue pipeline for project %s", project_id)
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not queue pipeline job (Redis/Celery unavailable). "
                "Start Redis and `celery -A workers.build_worker worker`, or set "
                "DF_PIPELINE_ENQUEUE_MODE=auto (default) or background in .env for local development."
            ),
        ) from exc
