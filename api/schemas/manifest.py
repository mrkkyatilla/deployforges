from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from api.schemas.project import CreateProjectRequest, UsageInfo


class DeploymentManifestResult(BaseModel):
    """v2 primary payload — mirrors core.manifest.DeploymentManifest JSON."""

    deployment_manifest_version: str = "1"
    project: dict[str, Any] = Field(default_factory=dict)
    services: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ProjectV2ResultResponse(BaseModel):
    id: UUID
    status: str
    deployment_manifest: DeploymentManifestResult | None = None
    usage: UsageInfo | None = None
    error_summary: str | None = None


class ProjectV2StatusResponse(BaseModel):
    id: UUID
    status: str
    manifest_version: str | None = "1"
    language: str | None = None
    framework: str | None = None
    is_monorepo: bool = False
    service_count: int = 0
    primary_service: str | None = None
    total_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime
    error_summary: str | None = None
    links: dict[str, str] = Field(default_factory=dict)


class CreateProjectV2Request(CreateProjectRequest):
    """Same intake as v1; pipeline uses DF_PIPELINE_MODE."""

    pass
