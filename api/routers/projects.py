from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import aiofiles
import redis.asyncio as redis
from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.config import settings
from api.middleware.abuse import check_abuse
from api.middleware.auth import AuthenticatedUser, get_current_user
from api.schemas.project import (
    AnalysisSummary,
    CreateProjectRequest,
    CurrentBuildSummary,
    ProjectListResponse,
    ProjectOptions,
    ProjectResult,
    ProjectResultResponse,
    ProjectStatusResponse,
    ProjectSummary,
    UsageInfo,
)
from workers.build_worker import run_pipeline_task
from db.models import AIInteraction, Build, Project
from db.session import get_db

logger = logging.getLogger(__name__)

_ALLOWED_UPLOAD_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz"}


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _queued_project_payload(project: Project, created_at: datetime) -> dict[str, object]:
    """202 body as plain JSON (avoids Pydantic ``self`` / response_model edge cases)."""
    base = f"/projects/{project.id}"
    return {
        "id": str(project.id),
        "status": project.status,
        "created_at": _iso_z(created_at),
        "estimated_duration_seconds": 180,
        "links": {
            "self": base,
            "builds": f"{base}/builds",
            "events": f"{base}/events",
        },
    }


def _enqueue_pipeline(project_id: UUID) -> None:
    try:
        run_pipeline_task.delay(str(project_id))
    except Exception as exc:
        logger.exception("Failed to enqueue pipeline for project %s", project_id)
        raise HTTPException(
            status_code=503,
            detail="Could not queue pipeline job (Redis/Celery unavailable).",
        ) from exc


router = APIRouter(prefix="/projects", tags=["projects"])


async def _get_user_project(
    project_id: UUID,
    user: AuthenticatedUser,
    db: AsyncSession,
    *,
    load_builds: bool = False,
) -> Project:
    stmt = select(Project).where(
        Project.id == project_id,
        Project.user_id == user.user_id,
    )
    if load_builds:
        stmt = stmt.options(selectinload(Project.builds))
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/", status_code=202)
async def create_project(
    payload: CreateProjectRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _abuse: None = Depends(check_abuse),
) -> JSONResponse:
    project = Project(
        user_id=user.user_id,
        source_type=payload.source.type,
        source_url=payload.source.url,
        source_branch=payload.source.branch,
        source_commit=payload.source.commit,
        status="queued",
    )
    db.add(project)
    await db.flush()

    project.workspace_path = str(settings.workspace_base_path / str(project.id))

    await db.flush()

    await db.refresh(project)

    created_at = project.created_at or datetime.now(timezone.utc)

    payload = _queued_project_payload(project, created_at)
    _enqueue_pipeline(project.id)

    return JSONResponse(status_code=202, content=payload)


@router.post("/upload", status_code=202)
async def upload_project(
    file: UploadFile,
    options: str = Form("{}"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _abuse: None = Depends(check_abuse),
) -> JSONResponse:
    max_bytes = settings.max_upload_size_mb * 1024 * 1024

    filename = file.filename or ""
    ext = _get_archive_extension(filename)
    if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(_ALLOWED_UPLOAD_EXTENSIONS))}",
        )

    try:
        parsed_options = ProjectOptions(**json.loads(options))
    except (json.JSONDecodeError, Exception) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid options JSON: {exc}") from exc

    source_type = "zip" if ext == ".zip" else "tar"

    project = Project(
        user_id=user.user_id,
        source_type=source_type,
        source_url=None,
        status="queued",
    )
    db.add(project)
    await db.flush()

    workspace = settings.workspace_base_path / str(project.id)
    workspace.mkdir(parents=True, exist_ok=True)
    project.workspace_path = str(workspace)
    dest = workspace / f"upload{ext}"

    total_written = 0
    try:
        async with aiofiles.open(dest, "wb") as f:
            while chunk := await file.read(256 * 1024):
                total_written += len(chunk)
                if total_written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds maximum size of {settings.max_upload_size_mb} MB",
                    )
                await f.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to save upload for project %s", project.id)
        raise HTTPException(status_code=500, detail="Failed to save uploaded file") from exc

    await db.flush()

    await db.refresh(project)

    created_at = project.created_at or datetime.now(timezone.utc)

    payload = _queued_project_payload(project, created_at)
    _enqueue_pipeline(project.id)
    logger.info("Upload project %s queued (source_type=%s, size=%d bytes)", project.id, source_type, total_written)

    return JSONResponse(status_code=202, content=payload)


def _get_archive_extension(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".tar.gz"):
        return ".tar.gz"
    return Path(lower).suffix


@router.get("/", response_model=ProjectListResponse)
async def list_projects(
    status: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectListResponse:
    valid_statuses = {"queued", "processing", "cloning", "analyzing", "building", "success", "failed"}
    if status and status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status filter. Allowed: {', '.join(sorted(valid_statuses))}",
        )

    base = select(Project).where(Project.user_id == user.user_id)
    if status:
        base = base.where(Project.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(base.subquery())
    )
    total = count_result.scalar() or 0

    rows_result = await db.execute(
        base.order_by(Project.created_at.desc()).offset(offset).limit(limit)
    )
    projects = rows_result.scalars().all()

    data = []
    for p in projects:
        fp = {}
        if p.fingerprint:
            fp = p.fingerprint if isinstance(p.fingerprint, dict) else json.loads(p.fingerprint)
        data.append(
            ProjectSummary(
                id=p.id,
                source_type=p.source_type,
                source_url=p.source_url,
                status=p.status,
                language=fp.get("language"),
                framework=fp.get("framework"),
                total_tokens=p.total_tokens_used or 0,
                cost_usd=p.total_cost_usd or 0.0,
                created_at=p.created_at,
            )
        )

    return ProjectListResponse(
        data=data,
        pagination={
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        },
    )


@router.get("/{project_id}", response_model=ProjectStatusResponse)
async def get_project_status(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectStatusResponse:
    project = await _get_user_project(project_id, user, db, load_builds=True)

    analysis = None
    if project.fingerprint:
        fp = project.fingerprint if isinstance(project.fingerprint, dict) else json.loads(project.fingerprint)
        analysis = AnalysisSummary(
            language=fp.get("language"),
            language_version=fp.get("language_version"),
            framework=fp.get("framework"),
            confidence=fp.get("confidence", 0.0),
            services_detected=fp.get("services_detected", 1),
        )

    current_build = None
    if project.builds:
        latest = project.builds[-1]
        current_build = CurrentBuildSummary(
            id=latest.id,
            attempt=latest.attempt_number,
            status=latest.build_status,
        )

    return ProjectStatusResponse(
        id=project.id,
        status=project.status,
        source={
            "type": project.source_type,
            "url": project.source_url,
            "branch": project.source_branch,
            "commit": project.source_commit,
        },
        analysis=analysis,
        current_build=current_build,
        created_at=project.created_at,
        updated_at=project.updated_at,
        error_summary=project.error_summary,
    )


@router.get("/{project_id}/result", response_model=ProjectResultResponse)
async def get_project_result(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResultResponse:
    project = await _get_user_project(project_id, user, db, load_builds=True)

    if project.status not in ("success", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Project is still in '{project.status}' state",
        )

    result = None
    if project.final_dockerfile:
        build_stats = None
        if project.builds:
            successful = [b for b in project.builds if b.build_status == "success"]
            latest_success = successful[-1] if successful else None
            build_stats = {
                "total_attempts": len(project.builds),
                "final_image_size_mb": None,
                "build_duration_seconds": latest_success.duration_ms // 1000 if latest_success and latest_success.duration_ms else None,
                "estimated_runtime_memory_mb": None,
            }

        result = ProjectResult(
            dockerfile=project.final_dockerfile,
            dockerignore=project.final_dockerignore,
            docker_compose=project.final_compose,
            build_stats=build_stats,
        )

    usage = None
    interactions_result = await db.execute(
        select(AIInteraction).where(AIInteraction.project_id == project_id)
    )
    interactions = interactions_result.scalars().all()
    if interactions:
        total_tokens = sum(
            (i.prompt_tokens or 0) + (i.completion_tokens or 0) for i in interactions
        )
        total_build_time = sum(
            b.duration_ms or 0 for b in (project.builds or [])
        ) // 1000
        total_cost = project.total_cost_usd or 0.0
        usage = UsageInfo(
            total_tokens=total_tokens,
            total_build_time_seconds=total_build_time,
            cost_estimate_usd=total_cost,
        )

    return ProjectResultResponse(
        id=project.id,
        status=project.status,
        result=result,
        usage=usage,
        error_summary=project.error_summary,
    )


@router.get("/{project_id}/events")
async def stream_project_events(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    await _get_user_project(project_id, user, db)

    async def event_generator():
        r = redis.from_url(settings.redis_url)
        pubsub = r.pubsub()
        channel = f"project:{project_id}:events"
        await pubsub.subscribe(channel)
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=30.0
                )
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    yield f"data: {data}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.1)
        finally:
            await pubsub.unsubscribe(channel)
            await r.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
