from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone

from api.config import settings
from workers.build_worker import celery_app

logger = logging.getLogger(__name__)

CLOUD_RUN_SERVICE_MAX_AGE_SECONDS = 30 * 60


@celery_app.task(name="deployforge.cleanup_stale_resources")
def cleanup_stale_resources() -> dict:
    """Remove workspace directories older than the configured retention period."""
    base = settings.workspace_base_path
    if not base.exists():
        logger.debug("Workspace base path %s does not exist, nothing to clean", base)
        return {"deleted": 0}

    cutoff = time.time() - (settings.artifact_retention_hours * 3600)
    deleted = 0

    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                logger.info("Deleted stale workspace: %s", entry.name)
                deleted += 1
        except Exception:
            logger.exception("Failed to delete workspace %s", entry)

    logger.info("Workspace cleanup finished: %d directories removed", deleted)
    return {"deleted": deleted}


@celery_app.task(name="deployforge.cleanup_cloud_run_services")
def cleanup_cloud_run_services() -> dict:
    """Delete ephemeral Cloud Run validation services older than 30 minutes."""
    region = settings.gcp_region

    try:
        result = subprocess.run(
            [
                "gcloud", "run", "services", "list",
                "--region", region,
                "--filter", "metadata.name~validate-",
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.exception("Failed to list Cloud Run services in %s", region)
        return {"deleted": 0, "error": "list command failed"}

    try:
        services = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        logger.exception("Failed to parse Cloud Run service list output")
        return {"deleted": 0, "error": "json parse failed"}

    now = datetime.now(timezone.utc)
    deleted = 0

    for svc in services:
        name = svc.get("metadata", {}).get("name", "")
        created_str = svc.get("metadata", {}).get("creationTimestamp", "")
        if not created_str:
            continue

        try:
            created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Unparseable creation timestamp for service %s: %s", name, created_str)
            continue

        age_seconds = (now - created_at).total_seconds()
        if age_seconds <= CLOUD_RUN_SERVICE_MAX_AGE_SECONDS:
            continue

        try:
            subprocess.run(
                [
                    "gcloud", "run", "services", "delete", name,
                    "--region", region,
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            logger.info("Deleted stale Cloud Run service: %s (age: %ds)", name, int(age_seconds))
            deleted += 1
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            logger.exception("Failed to delete Cloud Run service %s", name)

    logger.info("Cloud Run cleanup finished: %d services removed", deleted)
    return {"deleted": deleted}


celery_app.conf.beat_schedule = {
    "cleanup-workspaces": {
        "task": "deployforge.cleanup_stale_resources",
        "schedule": 300.0,
    },
    "cleanup-cloud-run": {
        "task": "deployforge.cleanup_cloud_run_services",
        "schedule": 300.0,
    },
}
