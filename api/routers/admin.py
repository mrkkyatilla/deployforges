from __future__ import annotations

import logging
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from core.monitoring import TokenMonitor
from db.models import AIInteraction, Build, CreditTransaction, Project, User
from db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def require_admin_key(
    key: str | None = Security(admin_key_header),
) -> str:
    if not settings.admin_api_key:
        raise HTTPException(status_code=501, detail="Admin API not configured")
    if not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    return key


@router.get("/stats")
async def system_stats(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> dict:
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    total_projects = (await db.execute(select(func.count(Project.id)))).scalar() or 0
    total_builds = (await db.execute(select(func.count(Build.id)))).scalar() or 0

    successful_builds = (
        await db.execute(
            select(func.count(Build.id)).where(Build.build_status == "success")
        )
    ).scalar() or 0
    success_rate = (successful_builds / total_builds * 100) if total_builds else 0.0

    avg_tokens = (
        await db.execute(select(func.avg(Project.total_tokens_used)))
    ).scalar() or 0.0

    total_revenue = (
        await db.execute(
            select(func.sum(CreditTransaction.amount)).where(
                CreditTransaction.transaction_type == "purchase"
            )
        )
    ).scalar() or 0.0

    active_projects = (
        await db.execute(
            select(func.count(Project.id)).where(
                Project.status.in_(["queued", "cloning", "analyzing", "building"])
            )
        )
    ).scalar() or 0

    return {
        "total_users": total_users,
        "total_projects": total_projects,
        "total_builds": total_builds,
        "success_rate": round(success_rate, 2),
        "avg_tokens_per_project": round(float(avg_tokens), 1),
        "total_revenue": round(float(total_revenue), 2),
        "active_projects": active_projects,
    }


@router.get("/projects/{project_id}/ai-interactions")
async def list_project_ai_interactions(
    project_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> dict:
    """Return recent Gemini interaction rows (tokens + optional ``extra`` excerpts)."""
    proj = await db.get(Project, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    result = await db.execute(
        select(AIInteraction)
        .where(AIInteraction.project_id == project_id)
        .order_by(AIInteraction.created_at.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    return {
        "project_id": str(project_id),
        "count": len(rows),
        "interactions": [
            {
                "id": str(r.id),
                "interaction_type": r.interaction_type,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "model_used": r.model_used,
                "latency_ms": r.latency_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "extra": r.extra,
            }
            for r in rows
        ],
    }


@router.get("/health/detailed")
async def detailed_health(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> dict:
    db_ok = False
    db_error = None
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    redis_ok = False
    redis_error = None
    try:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        await r.ping()
        await r.aclose()
        redis_ok = True
    except Exception as exc:
        redis_error = str(exc)

    celery_ok = False
    celery_error = None
    try:
        from workers.build_worker import celery_app

        inspector = celery_app.control.inspect(timeout=3.0)
        pong = inspector.ping()
        celery_ok = bool(pong)
        if not celery_ok:
            celery_error = "No workers responded"
    except Exception as exc:
        celery_error = str(exc)

    disk = shutil.disk_usage(str(settings.workspace_base_path))

    return {
        "database": {"ok": db_ok, "error": db_error},
        "redis": {"ok": redis_ok, "error": redis_error},
        "celery": {"ok": celery_ok, "error": celery_error},
        "disk": {
            "path": str(settings.workspace_base_path),
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "usage_percent": round(disk.used / disk.total * 100, 1),
        },
    }


@router.post("/reporter/run")
async def run_admin_reporter(
    period_days: int = Query(7, ge=1, le=90),
    include_llm: bool = Query(False),
    _: str = Depends(require_admin_key),
) -> dict:
    """Deterministic token/usage report plus optional LLM summary. Admin API key only — not callable with X-API-Key."""
    from core.ai.reporter_agent import run_reporter_report

    try:
        return await run_reporter_report(period_days=period_days, include_llm=include_llm)
    except Exception:
        logger.exception("Admin reporter run failed")
        raise HTTPException(status_code=500, detail="Reporter run failed")


@router.get("/monitoring/report")
async def get_monitoring_report(
    period_days: int = Query(30, ge=1, le=90),
    _: str = Depends(require_admin_key),
) -> dict:
    now = datetime.now(UTC)
    period_start = now - timedelta(days=period_days)

    monitor = TokenMonitor()
    try:
        report = await monitor.generate_report(period_start, now)
    except Exception:
        logger.exception("Failed to generate monitoring report")
        raise HTTPException(status_code=500, detail="Failed to generate monitoring report")

    suggestions = await monitor.get_optimization_suggestions(report)

    return {
        "report": {
            "period": report.period,
            "total_projects": report.total_projects,
            "total_tokens": report.total_tokens,
            "total_cost_usd": report.total_cost_usd,
            "per_model_breakdown": report.per_model_breakdown,
            "per_step_breakdown": report.per_step_breakdown,
            "avg_tokens_per_project": report.avg_tokens_per_project,
            "avg_cost_per_project": report.avg_cost_per_project,
            "cache_hit_rate": report.cache_hit_rate,
            "first_attempt_success_rate": report.first_attempt_success_rate,
            "top_token_consumers": report.top_token_consumers,
        },
        "alerts": [asdict(a) for a in report.alerts],
        "suggestions": suggestions,
    }
