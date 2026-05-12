from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import case, cast, func, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.middleware.auth import AuthenticatedUser, get_current_user
from api.schemas.trace import (
    CommonError,
    LanguageBreakdown,
    PipelineTraceResponse,
    StepTraceResponse,
    TraceStatsResponse,
)
from db.models import PipelineRun, Project
from db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/traces", tags=["traces"])

admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def _require_admin_key(
    key: str | None = Security(admin_key_header),
) -> str:
    if not settings.admin_api_key:
        raise HTTPException(status_code=501, detail="Admin API not configured")
    if not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    return key


@router.get("/stats", response_model=TraceStatsResponse)
async def trace_stats(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(_require_admin_key),
) -> TraceStatsResponse:
    avg_dur = (
        await db.execute(select(func.avg(PipelineRun.total_duration_ms)))
    ).scalar() or 0.0
    avg_tok = (
        await db.execute(select(func.avg(PipelineRun.total_tokens)))
    ).scalar() or 0.0

    total_runs = (
        await db.execute(select(func.count(PipelineRun.id)))
    ).scalar() or 0
    successful = (
        await db.execute(
            select(func.count(PipelineRun.id)).where(
                PipelineRun.final_status == "completed"
            )
        )
    ).scalar() or 0
    success_rate = (successful / total_runs * 100) if total_runs else 0.0

    lang_rows = (
        await db.execute(
            select(
                cast(Project.fingerprint["language"].astext, String).label("language"),
                func.count(PipelineRun.id).label("cnt"),
                func.avg(PipelineRun.total_duration_ms).label("avg_dur"),
                func.avg(PipelineRun.total_tokens).label("avg_tok"),
                func.sum(
                    case(
                        (PipelineRun.final_status == "completed", 1),
                        else_=0,
                    )
                ).label("ok"),
            )
            .join(Project, Project.id == PipelineRun.project_id)
            .where(Project.fingerprint.isnot(None))
            .group_by("language")
        )
    ).all()

    language_breakdown = [
        LanguageBreakdown(
            language=row.language or "unknown",
            count=row.cnt,
            avg_duration_ms=round(float(row.avg_dur or 0), 1),
            avg_tokens=round(float(row.avg_tok or 0), 1),
            success_rate=round(row.ok / row.cnt * 100, 1) if row.cnt else 0.0,
        )
        for row in lang_rows
    ]

    error_rows = (
        await db.execute(
            select(
                PipelineRun.steps,
            ).where(PipelineRun.final_status == "failed")
            .limit(500)
        )
    ).scalars().all()

    error_counts: dict[str, int] = {}
    for steps_json in error_rows:
        if not steps_json:
            continue
        for step in steps_json:
            for err in step.get("errors", []):
                trimmed = err[:200]
                error_counts[trimmed] = error_counts.get(trimmed, 0) + 1

    common_errors = sorted(
        [CommonError(error=e, count=c) for e, c in error_counts.items()],
        key=lambda x: x.count,
        reverse=True,
    )[:20]

    return TraceStatsResponse(
        avg_duration_ms=round(float(avg_dur), 1),
        avg_tokens=round(float(avg_tok), 1),
        success_rate=round(success_rate, 2),
        language_breakdown=language_breakdown,
        common_errors=common_errors,
    )


@router.get("/{project_id}", response_model=PipelineTraceResponse)
async def get_project_trace(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PipelineTraceResponse:
    run = await _get_latest_run(db, project_id, user)
    return _run_to_response(run)


@router.get("/{project_id}/steps", response_model=list[StepTraceResponse])
async def get_project_steps(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[StepTraceResponse]:
    run = await _get_latest_run(db, project_id, user)
    return [_step_dict_to_response(s) for s in (run.steps or [])]


async def _get_latest_run(
    db: AsyncSession,
    project_id: UUID,
    user: AuthenticatedUser,
) -> PipelineRun:
    project = (
        await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.user_id == user.user_id,
            )
        )
    ).scalar_one_or_none()

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    run = (
        await db.execute(
            select(PipelineRun)
            .where(PipelineRun.project_id == project_id)
            .order_by(PipelineRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(
            status_code=404,
            detail="No pipeline trace found for this project",
        )
    return run


def _run_to_response(run: PipelineRun) -> PipelineTraceResponse:
    return PipelineTraceResponse(
        project_id=str(run.project_id),
        started_at=run.started_at,
        completed_at=run.completed_at,
        total_duration_ms=run.total_duration_ms or 0,
        total_tokens=run.total_tokens or 0,
        total_cost_usd=run.total_cost_usd or 0.0,
        final_status=run.final_status or "unknown",
        steps=[_step_dict_to_response(s) for s in (run.steps or [])],
    )


def _step_dict_to_response(step: dict) -> StepTraceResponse:
    return StepTraceResponse(
        name=step.get("name", ""),
        started_at=step["started_at"],
        completed_at=step.get("completed_at"),
        duration_ms=step.get("duration_ms", 0),
        status=step.get("status", "unknown"),
        tokens_used=step.get("tokens_used"),
        model_used=step.get("model_used"),
        errors=step.get("errors", []),
    )
