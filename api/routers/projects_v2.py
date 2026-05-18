from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.config import settings
from api.middleware.abuse import check_abuse
from api.middleware.auth import AuthenticatedUser, get_current_user
from api.schemas.manifest import (
    CreateProjectV2Request,
    DeploymentManifestResult,
    ProjectV2ResultResponse,
    ProjectV2StatusResponse,
)
from api.schemas.project import UsageInfo
from core.manifest.schema import DeploymentManifest
from db.models import AIInteraction, Project
from db.session import get_db
from workers.pipeline_enqueue import schedule_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects-v2"])


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _manifest_from_project(project: Project) -> DeploymentManifestResult | None:
    raw = project.final_manifest
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    m = DeploymentManifest.from_dict(raw)
    return DeploymentManifestResult(**m.model_dump(mode="json"))


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
async def create_project_v2(
    payload: CreateProjectV2Request,
    background_tasks: BackgroundTasks,
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
        manifest_version="1",
    )
    db.add(project)
    try:
        await db.flush()
        project.workspace_path = str(settings.workspace_base_path / str(project.id))
        await db.flush()
        await db.refresh(project)
    except Exception as exc:
        await db.rollback()
        err = str(exc).lower()
        logger.exception("create_project_v2 DB flush failed")
        if "manifest_version" in err or "final_manifest" in err or "undefinedcolumn" in err:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Database schema is missing v2 columns (final_manifest, manifest_version). "
                    "On the VPS run: alembic upgrade head, then rebuild and restart api + celery-worker."
                ),
            ) from exc
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create project: {exc!s}",
        ) from exc

    created_at = project.created_at or datetime.now(timezone.utc)
    base = f"/api/v2/projects/{project.id}"
    body = {
        "id": str(project.id),
        "status": project.status,
        "manifest_version": project.manifest_version or "1",
        "pipeline_mode": settings.pipeline_mode,
        "created_at": _iso_z(created_at),
        "estimated_duration_seconds": 240 if settings.pipeline_mode == "multi_service" else 180,
        "links": {
            "self": base,
            "result": f"{base}/result",
            "events": f"/api/v1/projects/{project.id}/events",
        },
    }
    schedule_pipeline(project.id, background_tasks)
    return JSONResponse(status_code=202, content=body)


@router.get("/{project_id}", response_model=ProjectV2StatusResponse)
async def get_project_v2(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectV2StatusResponse:
    project = await _get_user_project(project_id, user, db)
    fp = project.fingerprint or {}
    services = fp.get("services") if isinstance(fp.get("services"), list) else []
    manifest = _manifest_from_project(project)
    primary = None
    if manifest and manifest.validation:
        primary = manifest.validation.get("primary_service")

    lang = None
    fw = None
    if isinstance(fp.get("language"), dict):
        lang = fp["language"].get("primary")
    if isinstance(fp.get("framework"), dict):
        fw = fp["framework"].get("name")

    base = f"/api/v2/projects/{project.id}"
    return ProjectV2StatusResponse(
        id=project.id,
        status=project.status,
        manifest_version=project.manifest_version or "1",
        language=lang,
        framework=fw,
        is_monorepo=bool(fp.get("is_monorepo")),
        service_count=len(services),
        primary_service=primary,
        total_tokens=project.total_tokens_used or 0,
        cost_usd=project.total_cost_usd or 0.0,
        created_at=project.created_at,
        updated_at=project.updated_at,
        error_summary=project.error_summary,
        links={
            "self": base,
            "result": f"{base}/result",
            "events": f"/api/v1/projects/{project.id}/events",
        },
    )


@router.get("/{project_id}/result", response_model=ProjectV2ResultResponse)
async def get_project_result_v2(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectV2ResultResponse:
    project = await _get_user_project(project_id, user, db, load_builds=True)

    if project.status not in ("success", "partial", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Project is still in '{project.status}' state",
        )

    manifest = _manifest_from_project(project)

    usage = None
    interactions_result = await db.execute(
        select(AIInteraction).where(AIInteraction.project_id == project_id)
    )
    interactions = interactions_result.scalars().all()
    if interactions or project.builds:
        total_tokens = sum(
            (i.prompt_tokens or 0) + (i.completion_tokens or 0) for i in interactions
        )
        total_build_time = sum(b.duration_ms or 0 for b in (project.builds or [])) // 1000
        usage = UsageInfo(
            total_tokens=total_tokens or project.total_tokens_used or 0,
            total_build_time_seconds=total_build_time,
            cost_estimate_usd=project.total_cost_usd or 0.0,
        )

    return ProjectV2ResultResponse(
        id=project.id,
        status=project.status,
        deployment_manifest=manifest,
        usage=usage,
        error_summary=project.error_summary,
    )
