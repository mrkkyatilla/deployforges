from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AIInteraction, CreditTransaction, Project, User
from db.session import async_session_factory

logger = logging.getLogger(__name__)

TIER_CONFIG = {
    "free": {"credit_price": 0.0, "max_credits": 5, "rate_per_credit": 0.15},
    "starter": {"credit_price": 0.15, "max_credits": None, "rate_per_credit": 0.15},
    "builder": {
        "credit_price": 0.10,
        "max_credits": None,
        "rate_per_credit": 0.10,
        "volume_discounts": {50: 0.08, 200: 0.06},
    },
    "scale": {"credit_price": 0.06, "max_credits": None, "rate_per_credit": 0.06},
}


@dataclass
class CreditCheck:
    has_credits: bool
    balance: float
    credits_needed: float
    tier: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class DeductResult:
    success: bool
    new_balance: float
    transaction_id: UUID | None = None


@dataclass
class DailyBreakdown:
    date: str
    projects: int
    credits: float
    cost: float


@dataclass
class UsageReport:
    period_start: datetime
    period_end: datetime
    credits_used: float
    credits_remaining: float
    cost_so_far_usd: float
    daily_breakdown: list[DailyBreakdown]
    total_projects: int
    total_credits_lifetime: float
    total_spent_usd: float


@dataclass
class CostBreakdown:
    project_id: UUID
    analysis_tokens: int
    generation_tokens: int
    fix_tokens: int
    total_tokens: int
    ai_cost_usd: float
    infra_cost_usd: float
    total_cost_usd: float
    credit_charged: float


class BillingService:
    async def check_credits(self, user_id: UUID) -> CreditCheck:
        async with async_session_factory() as db:
            result = await db.execute(
                select(User.credits_balance, User.tier).where(User.id == user_id)
            )
            row = result.one_or_none()
            if not row:
                raise ValueError(f"User {user_id} not found")

            balance, tier = row
            tier_cfg = TIER_CONFIG.get(tier, TIER_CONFIG["free"])
            credits_needed = 1.0
            has_credits = balance >= credits_needed

            warnings: list[str] = []
            if not has_credits:
                warnings.append("Insufficient credits. Please add credits to continue.")
            elif balance <= 2.0:
                warnings.append(f"Low balance: {balance:.1f} credits remaining.")
            if tier == "free" and tier_cfg["max_credits"] is not None:
                warnings.append(
                    f"Free tier limited to {tier_cfg['max_credits']} credits. "
                    "Upgrade for unlimited usage."
                )

            return CreditCheck(
                has_credits=has_credits,
                balance=balance,
                credits_needed=credits_needed,
                tier=tier,
                warnings=warnings,
            )

    async def deduct_credit(
        self, user_id: UUID, project_id: UUID, amount: float = 1.0
    ) -> DeductResult:
        async with async_session_factory() as db:
            try:
                result = await db.execute(
                    text(
                        "UPDATE users SET credits_balance = credits_balance - :amount "
                        "WHERE id = :id AND credits_balance >= :amount "
                        "RETURNING credits_balance"
                    ),
                    {"amount": amount, "id": user_id},
                )
                row = result.fetchone()
                if not row:
                    logger.warning(
                        "Credit deduction failed for user=%s amount=%.2f",
                        user_id, amount,
                    )
                    user_row = await db.execute(
                        select(User.credits_balance).where(User.id == user_id)
                    )
                    current = user_row.scalar() or 0.0
                    return DeductResult(success=False, new_balance=current)

                new_balance = row[0]
                txn = CreditTransaction(
                    user_id=user_id,
                    project_id=project_id,
                    amount=-amount,
                    balance_after=new_balance,
                    transaction_type="deduct",
                    description=f"Project deployment: {project_id}",
                )
                db.add(txn)
                await db.flush()
                txn_id = txn.id

                await db.commit()
                logger.info(
                    "Deducted %.2f credits from user=%s balance=%.2f txn=%s",
                    amount, user_id, new_balance, txn_id,
                )
                return DeductResult(
                    success=True, new_balance=new_balance, transaction_id=txn_id
                )
            except Exception:
                await db.rollback()
                raise

    async def refund_credit(
        self, user_id: UUID, project_id: UUID, amount: float, reason: str
    ) -> None:
        async with async_session_factory() as db:
            try:
                result = await db.execute(
                    text(
                        "UPDATE users SET credits_balance = credits_balance + :amount "
                        "WHERE id = :id RETURNING credits_balance"
                    ),
                    {"amount": amount, "id": user_id},
                )
                row = result.fetchone()
                if not row:
                    raise ValueError(f"User {user_id} not found for refund")

                new_balance = row[0]
                txn = CreditTransaction(
                    user_id=user_id,
                    project_id=project_id,
                    amount=amount,
                    balance_after=new_balance,
                    transaction_type="refund",
                    description=f"Refund for project {project_id}: {reason}",
                )
                db.add(txn)
                await db.commit()
                logger.info(
                    "Refunded %.2f credits to user=%s reason=%s",
                    amount, user_id, reason,
                )
            except Exception:
                await db.rollback()
                raise

    async def add_credits(
        self, user_id: UUID, amount: float, payment_method: str = "manual"
    ) -> CreditTransaction:
        async with async_session_factory() as db:
            try:
                result = await db.execute(
                    text(
                        "UPDATE users SET credits_balance = credits_balance + :amount "
                        "WHERE id = :id RETURNING credits_balance"
                    ),
                    {"amount": amount, "id": user_id},
                )
                row = result.fetchone()
                if not row:
                    raise ValueError(f"User {user_id} not found")

                new_balance = row[0]
                txn_type = "free_credit" if payment_method == "free_tier" else "purchase"
                txn = CreditTransaction(
                    user_id=user_id,
                    amount=amount,
                    balance_after=new_balance,
                    transaction_type=txn_type,
                    description=f"Added {amount:.1f} credits via {payment_method}",
                )
                db.add(txn)
                await db.flush()
                await db.commit()
                logger.info(
                    "Added %.2f credits to user=%s via %s balance=%.2f",
                    amount, user_id, payment_method, new_balance,
                )
                return txn
            except Exception:
                await db.rollback()
                raise

    async def get_usage(
        self,
        user_id: UUID,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> UsageReport:
        now = datetime.now(timezone.utc)
        if period_start is None:
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period_end is None:
            period_end = now

        async with async_session_factory() as db:
            user_row = await db.execute(
                select(User.credits_balance).where(User.id == user_id)
            )
            credits_remaining = user_row.scalar() or 0.0

            # Period transactions
            period_txns = await db.execute(
                select(CreditTransaction)
                .where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.transaction_type == "deduct",
                    CreditTransaction.created_at >= period_start,
                    CreditTransaction.created_at <= period_end,
                )
                .order_by(CreditTransaction.created_at)
            )
            txns = period_txns.scalars().all()
            credits_used = sum(abs(t.amount) for t in txns)

            # Daily breakdown
            daily: dict[str, DailyBreakdown] = {}
            for t in txns:
                day_key = t.created_at.strftime("%Y-%m-%d")
                if day_key not in daily:
                    daily[day_key] = DailyBreakdown(
                        date=day_key, projects=0, credits=0.0, cost=0.0
                    )
                daily[day_key].projects += 1
                daily[day_key].credits += abs(t.amount)

            # Compute cost per day using user's tier
            user_tier_row = await db.execute(
                select(User.tier).where(User.id == user_id)
            )
            tier = user_tier_row.scalar() or "free"
            rate = TIER_CONFIG.get(tier, TIER_CONFIG["free"])["rate_per_credit"]
            for d in daily.values():
                d.cost = round(d.credits * rate, 4)
            cost_so_far = round(credits_used * rate, 4)

            # Lifetime stats
            lifetime_txns = await db.execute(
                select(
                    func.count(CreditTransaction.id),
                    func.coalesce(func.sum(func.abs(CreditTransaction.amount)), 0.0),
                ).where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.transaction_type == "deduct",
                )
            )
            lifetime_row = lifetime_txns.one()
            total_projects = lifetime_row[0]
            total_credits = float(lifetime_row[1])

            lifetime_purchases = await db.execute(
                select(
                    func.coalesce(func.sum(CreditTransaction.amount), 0.0)
                ).where(
                    CreditTransaction.user_id == user_id,
                    CreditTransaction.transaction_type.in_(["purchase", "free_credit"]),
                )
            )
            total_spent_usd = round(float(lifetime_purchases.scalar() or 0.0) * rate, 4)

            return UsageReport(
                period_start=period_start,
                period_end=period_end,
                credits_used=credits_used,
                credits_remaining=credits_remaining,
                cost_so_far_usd=cost_so_far,
                daily_breakdown=sorted(daily.values(), key=lambda d: d.date),
                total_projects=total_projects,
                total_credits_lifetime=total_credits,
                total_spent_usd=round(total_credits * rate, 4),
            )

    async def get_cost_breakdown(self, project_id: UUID) -> CostBreakdown:
        async with async_session_factory() as db:
            interactions_result = await db.execute(
                select(AIInteraction).where(AIInteraction.project_id == project_id)
            )
            interactions = interactions_result.scalars().all()

            analysis_tokens = 0
            generation_tokens = 0
            fix_tokens = 0

            for i in interactions:
                tokens = (i.prompt_tokens or 0) + (i.completion_tokens or 0)
                itype = (i.interaction_type or "").lower()
                if "analysis" in itype or "fingerprint" in itype:
                    analysis_tokens += tokens
                elif "fix" in itype or "error" in itype:
                    fix_tokens += tokens
                else:
                    generation_tokens += tokens

            total_tokens = analysis_tokens + generation_tokens + fix_tokens

            # ~$0.01 per 1k tokens (blended Gemini rate)
            ai_cost_usd = round(total_tokens * 0.00001, 6)
            infra_cost_usd = round(0.005, 6)  # flat per-project build infra cost
            total_cost_usd = round(ai_cost_usd + infra_cost_usd, 6)

            txn_result = await db.execute(
                select(func.coalesce(func.sum(func.abs(CreditTransaction.amount)), 0.0))
                .where(
                    CreditTransaction.project_id == project_id,
                    CreditTransaction.transaction_type == "deduct",
                )
            )
            credit_charged = float(txn_result.scalar() or 0.0)

            return CostBreakdown(
                project_id=project_id,
                analysis_tokens=analysis_tokens,
                generation_tokens=generation_tokens,
                fix_tokens=fix_tokens,
                total_tokens=total_tokens,
                ai_cost_usd=ai_cost_usd,
                infra_cost_usd=infra_cost_usd,
                total_cost_usd=total_cost_usd,
                credit_charged=credit_charged,
            )
