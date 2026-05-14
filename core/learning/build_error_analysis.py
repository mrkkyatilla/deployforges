"""Standard ``Build.error_analysis`` JSON shape (schema v1)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select

from db.models import Build
from db.session import async_session_factory

logger = logging.getLogger(__name__)

ERROR_ANALYSIS_SCHEMA_VERSION = "1"


def build_error_analysis_v1(
    *,
    classified_errors: list[dict[str, Any]],
    pipeline_policy: dict[str, Any] | None = None,
    fixes_applied: list[str] | None = None,
    deploy_error_excerpt: str | None = None,
) -> dict[str, Any]:
    """Build a versioned ``error_analysis`` payload for ``Build.error_analysis`` (JSONB)."""
    names = [str(e.get("name") or "") for e in classified_errors if e.get("name")]
    summary = "; ".join(names) if names else "unknown"
    payload: dict[str, Any] = {
        "schema_version": ERROR_ANALYSIS_SCHEMA_VERSION,
        "type": names[0] if names else None,
        "summary": summary,
        "classified": [
            {
                "name": e.get("name"),
                "fix_strategy": e.get("fix_strategy"),
                "auto_fixable": e.get("auto_fixable"),
                "error_type": e.get("error_type"),
            }
            for e in classified_errors
        ],
        "pipeline_policy": pipeline_policy or {},
    }
    if fixes_applied:
        payload["fixes_applied"] = list(fixes_applied)
    if deploy_error_excerpt:
        payload["deploy_error_excerpt"] = deploy_error_excerpt[:2000]
    return payload


def success_error_analysis_v1() -> dict[str, Any]:
    return {
        "schema_version": ERROR_ANALYSIS_SCHEMA_VERSION,
        "outcome": "success",
        "type": None,
        "summary": None,
    }


async def persist_error_analysis_for_attempt(
    project_id: UUID,
    attempt_number: int,
    payload: dict[str, Any],
) -> None:
    """Attach ``payload`` to the latest ``Build`` row for this project and attempt number."""
    try:
        async with async_session_factory() as db:
            r = await db.execute(
                select(Build)
                .where(
                    Build.project_id == project_id,
                    Build.attempt_number == attempt_number,
                )
                .order_by(desc(Build.created_at))
                .limit(1),
            )
            b = r.scalar_one_or_none()
            if not b:
                logger.debug(
                    "No Build row for project=%s attempt=%s — skip error_analysis persist",
                    project_id,
                    attempt_number,
                )
                return
            prev = dict(b.error_analysis) if isinstance(b.error_analysis, dict) else {}
            b.error_analysis = {**prev, **payload}
            await db.commit()
    except Exception:
        logger.warning("persist_error_analysis_for_attempt failed", exc_info=True)


async def merge_error_analysis_fixes(
    project_id: UUID,
    attempt_number: int,
    fixes_applied: list[str],
) -> None:
    """Append ``fixes_applied`` to the existing v1 ``error_analysis`` for that attempt."""
    if not fixes_applied:
        return
    try:
        async with async_session_factory() as db:
            r = await db.execute(
                select(Build)
                .where(
                    Build.project_id == project_id,
                    Build.attempt_number == attempt_number,
                )
                .order_by(desc(Build.created_at))
                .limit(1),
            )
            b = r.scalar_one_or_none()
            if not b:
                return
            cur = dict(b.error_analysis) if isinstance(b.error_analysis, dict) else {}
            prev = list(cur.get("fixes_applied") or [])
            cur["fixes_applied"] = prev + fixes_applied
            b.error_analysis = cur
            await db.commit()
    except Exception:
        logger.warning("merge_error_analysis_fixes failed", exc_info=True)


async def mark_latest_success_build_analysis(project_id: UUID) -> None:
    """Set a compact success marker on the most recent successful build row."""
    try:
        async with async_session_factory() as db:
            r = await db.execute(
                select(Build)
                .where(Build.project_id == project_id, Build.build_status == "success")
                .order_by(desc(Build.created_at))
                .limit(1),
            )
            b = r.scalar_one_or_none()
            if not b:
                return
            pol: dict[str, Any] = {}
            prev = b.error_analysis if isinstance(b.error_analysis, dict) else {}
            pp = prev.get("pipeline_policy")
            if isinstance(pp, dict):
                pol = pp
            doc = {**success_error_analysis_v1(), "pipeline_policy": pol}
            b.error_analysis = doc
            await db.commit()
    except Exception:
        logger.warning("mark_latest_success_build_analysis failed", exc_info=True)
