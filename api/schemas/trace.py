from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class StepTraceResponse(BaseModel):
    name: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int = 0
    status: str
    tokens_used: int | None = None
    model_used: str | None = None
    errors: list[str] = Field(default_factory=list)


class PipelineTraceResponse(BaseModel):
    project_id: str
    started_at: datetime
    completed_at: datetime | None = None
    total_duration_ms: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    final_status: str
    steps: list[StepTraceResponse] = Field(default_factory=list)


class LanguageBreakdown(BaseModel):
    language: str
    count: int
    avg_duration_ms: float
    avg_tokens: float
    success_rate: float


class CommonError(BaseModel):
    error: str
    count: int


class TraceStatsResponse(BaseModel):
    avg_duration_ms: float
    avg_tokens: float
    success_rate: float
    language_breakdown: list[LanguageBreakdown] = Field(default_factory=list)
    common_errors: list[CommonError] = Field(default_factory=list)
