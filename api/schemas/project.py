from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceInput(BaseModel):
    type: str = Field(..., pattern="^(git|zip|tar)$")
    url: str | None = None
    branch: str | None = "main"
    commit: str | None = None


class ProjectOptions(BaseModel):
    max_attempts: int = Field(default=5, ge=1, le=10)
    target_platform: str = "linux/amd64"
    skip_deploy_test: bool = False
    env_vars: dict[str, str] = Field(default_factory=dict)


class CreateProjectRequest(BaseModel):
    source: SourceInput
    options: ProjectOptions = Field(default_factory=ProjectOptions)
    webhook_url: str | None = None


class ProjectLinks(BaseModel):
    """HATEOAS-style relative URLs. JSON uses ``self`` (reserved in Python → ``self_``)."""

    model_config = ConfigDict(populate_by_name=True)

    self_: str = Field(alias="self")
    builds: str
    events: str


class CreateProjectResponse(BaseModel):
    id: UUID
    status: str
    created_at: datetime
    estimated_duration_seconds: int = 180
    links: ProjectLinks


class AnalysisSummary(BaseModel):
    language: str | None = None
    language_version: str | None = None
    framework: str | None = None
    confidence: float = 0.0
    services_detected: int = 1


class CurrentBuildSummary(BaseModel):
    id: UUID
    attempt: int
    status: str


class ProjectStatusResponse(BaseModel):
    id: UUID
    status: str
    source: SourceInput
    analysis: AnalysisSummary | None = None
    current_build: CurrentBuildSummary | None = None
    created_at: datetime
    updated_at: datetime
    # Populated when status is failed (or after worker crash) to aid debugging without DB access.
    error_summary: str | None = None


class BuildStats(BaseModel):
    total_attempts: int
    final_image_size_mb: float | None = None
    build_duration_seconds: int | None = None
    estimated_runtime_memory_mb: int | None = None


class DeployTestResult(BaseModel):
    url: str | None = None
    health_check: str | None = None
    response_time_ms: int | None = None
    expires_at: datetime | None = None


class UsageInfo(BaseModel):
    total_tokens: int
    total_build_time_seconds: int
    cost_estimate_usd: float


class ProjectResult(BaseModel):
    dockerfile: str
    dockerignore: str | None = None
    docker_compose: str | None = None
    analysis_summary: str | None = None
    warnings: list[str] = Field(default_factory=list)
    build_stats: BuildStats | None = None
    deploy_test: DeployTestResult | None = None


class ProjectResultResponse(BaseModel):
    id: UUID
    status: str
    result: ProjectResult | None = None
    usage: UsageInfo | None = None


class ProjectSummary(BaseModel):
    id: UUID
    source_type: str
    source_url: str | None = None
    status: str
    language: str | None = None
    framework: str | None = None
    total_tokens: int = 0
    cost_usd: float = 0.0
    created_at: datetime


class ProjectListResponse(BaseModel):
    data: list[ProjectSummary]
    pagination: dict
