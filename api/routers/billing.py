from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.middleware.auth import AuthenticatedUser, get_current_user
from api.schemas.billing import (
    AddCreditsRequest,
    CostBreakdownResponse,
    CreditCheckResponse,
    CurrentPeriod,
    DailyUsage,
    LifetimeStats,
    PaginationInfo,
    TransactionListResponse,
    TransactionResponse,
    UsageResponse,
)
from core.billing import BillingService
from db.models import CreditTransaction, Project
from db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

billing_service = BillingService()


@router.get("/credits", response_model=CreditCheckResponse)
async def check_credits(
    user: AuthenticatedUser = Depends(get_current_user),
) -> CreditCheckResponse:
    check = await billing_service.check_credits(user.user_id)
    return CreditCheckResponse(
        has_credits=check.has_credits,
        balance=check.balance,
        tier=check.tier,
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    user: AuthenticatedUser = Depends(get_current_user),
    period_start: datetime | None = Query(None),
    period_end: datetime | None = Query(None),
) -> UsageResponse:
    report = await billing_service.get_usage(
        user.user_id,
        period_start=period_start,
        period_end=period_end,
    )
    return UsageResponse(
        current_period=CurrentPeriod(
            start=report.period_start,
            end=report.period_end,
            credits_used=report.credits_used,
            credits_remaining=report.credits_remaining,
            cost_so_far_usd=report.cost_so_far_usd,
            breakdown=[
                DailyUsage(
                    date=d.date,
                    projects=d.projects,
                    credits=d.credits,
                    cost=d.cost,
                )
                for d in report.daily_breakdown
            ],
        ),
        lifetime=LifetimeStats(
            total_projects=report.total_projects,
            total_credits=report.total_credits_lifetime,
            total_spent_usd=report.total_spent_usd,
        ),
    )


@router.get("/usage/{project_id}", response_model=CostBreakdownResponse)
async def get_project_cost(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CostBreakdownResponse:
    result = await db.execute(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user.user_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")

    breakdown = await billing_service.get_cost_breakdown(project_id)
    return CostBreakdownResponse(
        project_id=breakdown.project_id,
        analysis_tokens=breakdown.analysis_tokens,
        generation_tokens=breakdown.generation_tokens,
        fix_tokens=breakdown.fix_tokens,
        total_tokens=breakdown.total_tokens,
        ai_cost_usd=breakdown.ai_cost_usd,
        infra_cost_usd=breakdown.infra_cost_usd,
        total_cost_usd=breakdown.total_cost_usd,
    )


@router.get("/transactions", response_model=TransactionListResponse)
async def list_transactions(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> TransactionListResponse:
    count_result = await db.execute(
        select(func.count(CreditTransaction.id)).where(
            CreditTransaction.user_id == user.user_id
        )
    )
    total = count_result.scalar() or 0

    offset = (page - 1) * per_page
    txn_result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user.user_id)
        .order_by(CreditTransaction.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    txns = txn_result.scalars().all()

    pages = max(1, (total + per_page - 1) // per_page)

    return TransactionListResponse(
        data=[
            TransactionResponse(
                id=t.id,
                amount=t.amount,
                balance_after=t.balance_after,
                transaction_type=t.transaction_type,
                description=t.description,
                created_at=t.created_at,
            )
            for t in txns
        ],
        pagination=PaginationInfo(
            total=total,
            page=page,
            per_page=per_page,
            pages=pages,
        ),
    )


@router.post("/credits/add", response_model=TransactionResponse, status_code=201)
async def add_credits(
    payload: AddCreditsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TransactionResponse:
    try:
        txn = await billing_service.add_credits(
            user.user_id, payload.amount, payment_method="manual"
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return TransactionResponse(
        id=txn.id,
        amount=txn.amount,
        balance_after=txn.balance_after,
        transaction_type=txn.transaction_type,
        description=txn.description,
        created_at=txn.created_at,
    )
