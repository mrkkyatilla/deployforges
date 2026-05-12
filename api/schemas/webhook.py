from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


VALID_EVENTS = frozenset({
    "project.queued",
    "project.analyzing",
    "project.building",
    "project.completed",
    "project.failed",
    "build.started",
    "build.succeeded",
    "build.failed",
})


class RegisterWebhookRequest(BaseModel):
    url: str
    events: list[str] = Field(..., min_length=1)
    secret: str | None = None


class WebhookResponse(BaseModel):
    id: UUID
    url: str
    events: list[str]
    is_active: bool
    created_at: datetime


class WebhookListResponse(BaseModel):
    data: list[WebhookResponse]


class InboundWebhookSetupRequest(BaseModel):
    provider: str = Field(..., pattern="^(github|gitlab)$")
    repo_url: str | None = None


class InboundWebhookSetupResponse(BaseModel):
    id: UUID
    provider: str
    webhook_secret: str
    webhook_url: str
