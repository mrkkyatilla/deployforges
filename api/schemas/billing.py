from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreditCheckResponse(BaseModel):
    has_credits: bool
    balance: float
    tier: str


class DailyUsage(BaseModel):
    date: str
    projects: int
    credits: float
    cost: float


class CurrentPeriod(BaseModel):
    start: datetime
    end: datetime
    credits_used: float
    credits_remaining: float
    cost_so_far_usd: float
    breakdown: list[DailyUsage] = Field(default_factory=list)


class LifetimeStats(BaseModel):
    total_projects: int
    total_credits: float
    total_spent_usd: float


class UsageResponse(BaseModel):
    current_period: CurrentPeriod
    lifetime: LifetimeStats


class CostBreakdownResponse(BaseModel):
    project_id: UUID
    analysis_tokens: int
    generation_tokens: int
    fix_tokens: int
    total_tokens: int
    ai_cost_usd: float
    infra_cost_usd: float
    total_cost_usd: float


class TransactionResponse(BaseModel):
    id: UUID
    amount: float
    balance_after: float
    transaction_type: str
    description: str | None = None
    created_at: datetime


class PaginationInfo(BaseModel):
    total: int
    page: int
    per_page: int
    pages: int


class TransactionListResponse(BaseModel):
    data: list[TransactionResponse]
    pagination: PaginationInfo


class AddCreditsRequest(BaseModel):
    amount: float = Field(..., ge=1.0, le=10000.0)
