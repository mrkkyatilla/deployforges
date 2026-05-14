from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.middleware.auth import AuthenticatedUser, get_current_user
from api.schemas.build import BuildListResponse, BuildLogResponse, BuildSummary, LogLine
from db.models import Build, Project
from db.session import get_db

router = APIRouter(prefix="/projects/{project_id}/builds", tags=["builds"])


async def _verify_project_ownership(
    project_id: UUID,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Project:
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user.user_id,
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/", response_model=BuildListResponse)
async def list_builds(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BuildListResponse:
    await _verify_project_ownership(project_id, user, db)

    result = await db.execute(
        select(Build)
        .where(Build.project_id == project_id)
        .order_by(Build.attempt_number)
    )
    builds = result.scalars().all()

    return BuildListResponse(
        data=[
            BuildSummary(
                id=b.id,
                attempt_number=b.attempt_number,
                status=b.build_status,
                error_type=b.error_analysis.get("type") if b.error_analysis else None,
                error_summary=b.error_analysis.get("summary") if b.error_analysis else None,
                error_analysis=dict(b.error_analysis) if b.error_analysis else None,
                duration_seconds=b.duration_ms // 1000 if b.duration_ms else None,
                image_size_mb=None,
                created_at=b.created_at,
            )
            for b in builds
        ],
        pagination={"has_more": False, "total": len(builds)},
    )


@router.get("/{build_id}/logs", response_model=BuildLogResponse)
async def get_build_logs(
    project_id: UUID,
    build_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BuildLogResponse:
    await _verify_project_ownership(project_id, user, db)

    result = await db.execute(
        select(Build).where(
            Build.id == build_id,
            Build.project_id == project_id,
        )
    )
    build = result.scalar_one_or_none()
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    log_lines: list[LogLine] = []
    if build.build_log:
        for line in build.build_log.splitlines():
            log_lines.append(LogLine(line=line))

    return BuildLogResponse(
        build_id=build.id,
        log_lines=log_lines,
        total_lines=len(log_lines),
        truncated=False,
    )
