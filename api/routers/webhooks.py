from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.middleware.auth import AuthenticatedUser, get_current_user
from api.schemas.webhook import (
    VALID_EVENTS,
    InboundWebhookSetupRequest,
    InboundWebhookSetupResponse,
    RegisterWebhookRequest,
    WebhookListResponse,
    WebhookResponse,
)
from core.webhooks import webhook_dispatcher
from db.models import InboundWebhookConfig, Project, Webhook
from db.session import get_db
from workers.pipeline_enqueue import schedule_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _branch_from_ref(ref: str) -> str:
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


# ---------------------------------------------------------------------------
# Inbound: GitHub push webhook
# ---------------------------------------------------------------------------

@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    body = await request.body()

    if x_github_event == "ping":
        return {"status": "pong"}

    if x_github_event != "push":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported GitHub event: {x_github_event}",
        )

    if not x_hub_signature_256:
        raise HTTPException(status_code=400, detail="Missing X-Hub-Signature-256 header")

    payload: dict = await request.json()

    configs_result = await db.execute(
        select(InboundWebhookConfig).where(
            InboundWebhookConfig.provider == "github",
            InboundWebhookConfig.is_active == 1,
        )
    )
    configs = configs_result.scalars().all()

    matched_config: InboundWebhookConfig | None = None
    for cfg in configs:
        if _verify_github_signature(body, x_hub_signature_256, cfg.webhook_secret):
            matched_config = cfg
            break

    if matched_config is None:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    repo = payload.get("repository", {})
    clone_url = repo.get("clone_url", "")
    ref = payload.get("ref", "")
    branch = _branch_from_ref(ref)
    head_commit = payload.get("head_commit", {})
    sha = head_commit.get("id", payload.get("after", ""))

    project = Project(
        user_id=matched_config.user_id,
        source_type="git",
        source_url=clone_url,
        source_branch=branch,
        source_commit=sha,
        status="queued",
        workspace_path=str(settings.workspace_base_path / "pending"),
    )
    db.add(project)
    await db.flush()

    project.workspace_path = str(settings.workspace_base_path / str(project.id))
    await db.flush()

    schedule_pipeline(project.id, background_tasks)

    await webhook_dispatcher.dispatch(
        matched_config.user_id,
        "project.queued",
        {"project_id": str(project.id), "source": "github", "branch": branch},
        db=db,
    )

    logger.info(
        "GitHub push accepted: repo=%s branch=%s project=%s",
        clone_url, branch, project.id,
    )
    return {"status": "accepted", "project_id": str(project.id)}


# ---------------------------------------------------------------------------
# Inbound: GitLab push webhook
# ---------------------------------------------------------------------------

@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not x_gitlab_token:
        raise HTTPException(status_code=400, detail="Missing X-Gitlab-Token header")

    config_result = await db.execute(
        select(InboundWebhookConfig).where(
            InboundWebhookConfig.provider == "gitlab",
            InboundWebhookConfig.webhook_secret == x_gitlab_token,
            InboundWebhookConfig.is_active == 1,
        )
    )
    matched_config = config_result.scalar_one_or_none()

    if matched_config is None:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    payload: dict = await request.json()

    gitlab_project = payload.get("project", {})
    clone_url = gitlab_project.get("git_http_url", "")
    ref = payload.get("ref", "")
    branch = _branch_from_ref(ref)
    sha = payload.get("checkout_sha", "")

    project = Project(
        user_id=matched_config.user_id,
        source_type="git",
        source_url=clone_url,
        source_branch=branch,
        source_commit=sha,
        status="queued",
        workspace_path=str(settings.workspace_base_path / "pending"),
    )
    db.add(project)
    await db.flush()

    project.workspace_path = str(settings.workspace_base_path / str(project.id))
    await db.flush()

    schedule_pipeline(project.id, background_tasks)

    await webhook_dispatcher.dispatch(
        matched_config.user_id,
        "project.queued",
        {"project_id": str(project.id), "source": "gitlab", "branch": branch},
        db=db,
    )

    logger.info(
        "GitLab push accepted: repo=%s branch=%s project=%s",
        clone_url, branch, project.id,
    )
    return {"status": "accepted", "project_id": str(project.id)}


# ---------------------------------------------------------------------------
# Inbound: Setup (register inbound webhook config)
# ---------------------------------------------------------------------------

@router.post("/inbound/setup", response_model=InboundWebhookSetupResponse)
async def setup_inbound_webhook(
    payload: InboundWebhookSetupRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InboundWebhookSetupResponse:
    webhook_secret = secrets.token_hex(32)

    config = InboundWebhookConfig(
        user_id=user.user_id,
        provider=payload.provider,
        webhook_secret=webhook_secret,
        repo_url=payload.repo_url,
    )
    db.add(config)
    await db.flush()

    webhook_url = f"/api/v1/webhooks/{payload.provider}"

    return InboundWebhookSetupResponse(
        id=config.id,
        provider=config.provider,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
    )


# ---------------------------------------------------------------------------
# Outbound: Register user webhook
# ---------------------------------------------------------------------------

@router.post("/register", response_model=WebhookResponse, status_code=201)
async def register_webhook(
    payload: RegisterWebhookRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookResponse:
    invalid = set(payload.events) - VALID_EVENTS
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid event types: {', '.join(sorted(invalid))}",
        )

    webhook = Webhook(
        user_id=user.user_id,
        url=payload.url,
        secret=payload.secret,
        events=payload.events,
    )
    db.add(webhook)
    await db.flush()

    return WebhookResponse(
        id=webhook.id,
        url=webhook.url,
        events=webhook.events,
        is_active=bool(webhook.is_active),
        created_at=webhook.created_at,
    )


# ---------------------------------------------------------------------------
# Outbound: List user webhooks
# ---------------------------------------------------------------------------

@router.get("", response_model=WebhookListResponse)
async def list_webhooks(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WebhookListResponse:
    result = await db.execute(
        select(Webhook).where(
            Webhook.user_id == user.user_id,
            Webhook.is_active == 1,
        ).order_by(Webhook.created_at.desc())
    )
    webhooks = result.scalars().all()

    return WebhookListResponse(
        data=[
            WebhookResponse(
                id=wh.id,
                url=wh.url,
                events=wh.events or [],
                is_active=bool(wh.is_active),
                created_at=wh.created_at,
            )
            for wh in webhooks
        ]
    )


# ---------------------------------------------------------------------------
# Outbound: Delete user webhook
# ---------------------------------------------------------------------------

@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(Webhook).where(
            Webhook.id == webhook_id,
            Webhook.user_id == user.user_id,
        )
    )
    webhook = result.scalar_one_or_none()

    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    webhook.is_active = 0
    await db.flush()
