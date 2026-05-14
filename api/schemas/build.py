from __future__ import annotations

from datetime import datetime
from uuid import UUID

from typing import Any

from pydantic import BaseModel, Field


class BuildSummary(BaseModel):
    id: UUID
    attempt_number: int
    status: str
    error_type: str | None = None
    error_summary: str | None = None
    # Full ``Build.error_analysis`` JSONB when present (schema_version 1 documented in README / runbook).
    error_analysis: dict[str, Any] | None = None
    duration_seconds: int | None = None
    image_size_mb: float | None = None
    created_at: datetime


class BuildListResponse(BaseModel):
    data: list[BuildSummary]
    pagination: dict = Field(default_factory=lambda: {"has_more": False, "total": 0})


class LogLine(BaseModel):
    timestamp: str | None = None
    line: str


class BuildLogResponse(BaseModel):
    build_id: UUID
    log_lines: list[LogLine]
    total_lines: int
    truncated: bool = False
