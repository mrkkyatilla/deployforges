from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache import DockerfileCache
from core.observability import COST_PER_MILLION_TOKENS
from db.models import AIInteraction, Build, Project
from db.session import async_session_factory

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    name: str
    severity: str
    message: str
    current_value: float
    threshold: float


@dataclass
class TokenUsageReport:
    period: str
    total_projects: int
    total_tokens: int
    total_cost_usd: float
    per_model_breakdown: dict[str, Any]
    per_step_breakdown: dict[str, Any]
    avg_tokens_per_project: float
    avg_cost_per_project: float
    cache_hit_rate: float
    first_attempt_success_rate: float
    top_token_consumers: list[dict[str, Any]]
    alerts: list[Alert] = field(default_factory=list)


class TokenMonitor:
    """Generates token usage reports, checks alerts, and suggests optimisations."""

    ALERT_HIGH_TOKEN_AVG = 30_000
    ALERT_LOW_CACHE_HIT = 0.10
    ALERT_HIGH_RETRY_RATE = 0.60
    ALERT_COST_SPIKE_MULTIPLIER = 2.0

    async def generate_report(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> TokenUsageReport:
        period_label = f"{period_start.date()} — {period_end.date()}"

        async with async_session_factory() as db:
            total_projects = await self._count_projects(db, period_start, period_end)
            interactions = await self._get_interactions(db, period_start, period_end)
            builds = await self._get_builds(db, period_start, period_end)
            top_consumers = await self._top_token_consumers(db, period_start, period_end)

        total_tokens = sum(
            (i.prompt_tokens or 0) + (i.completion_tokens or 0) for i in interactions
        )
        total_cost = self._calculate_cost(interactions)
        avg_tokens = total_tokens / total_projects if total_projects else 0.0
        avg_cost = total_cost / total_projects if total_projects else 0.0

        model_breakdown = self._per_model_breakdown(interactions)
        step_breakdown = self._per_step_breakdown(interactions)

        cache = DockerfileCache()
        try:
            stats = await cache.get_stats()
            cache_hit_rate = stats.hit_rate / 100.0 if stats.hit_rate else 0.0
        except Exception:
            logger.warning("Could not fetch cache stats; defaulting to 0")
            cache_hit_rate = 0.0
        finally:
            await cache.close()

        first_attempt_rate = self._first_attempt_success_rate(builds)

        report = TokenUsageReport(
            period=period_label,
            total_projects=total_projects,
            total_tokens=total_tokens,
            total_cost_usd=round(total_cost, 4),
            per_model_breakdown=model_breakdown,
            per_step_breakdown=step_breakdown,
            avg_tokens_per_project=round(avg_tokens, 1),
            avg_cost_per_project=round(avg_cost, 6),
            cache_hit_rate=round(cache_hit_rate, 4),
            first_attempt_success_rate=round(first_attempt_rate, 4),
            top_token_consumers=top_consumers,
        )

        report.alerts = await self.check_alerts(report)
        return report

    async def check_alerts(self, report: TokenUsageReport) -> list[Alert]:
        alerts: list[Alert] = []

        if report.avg_tokens_per_project > self.ALERT_HIGH_TOKEN_AVG:
            alerts.append(Alert(
                name="high_token_usage",
                severity="warning",
                message=(
                    f"Average tokens per project ({report.avg_tokens_per_project:.0f}) "
                    f"exceeds threshold ({self.ALERT_HIGH_TOKEN_AVG})"
                ),
                current_value=report.avg_tokens_per_project,
                threshold=float(self.ALERT_HIGH_TOKEN_AVG),
            ))

        if report.total_projects > 0 and report.cache_hit_rate < self.ALERT_LOW_CACHE_HIT:
            alerts.append(Alert(
                name="low_cache_hit",
                severity="warning",
                message=(
                    f"Cache hit rate ({report.cache_hit_rate:.1%}) "
                    f"is below threshold ({self.ALERT_LOW_CACHE_HIT:.0%})"
                ),
                current_value=report.cache_hit_rate,
                threshold=self.ALERT_LOW_CACHE_HIT,
            ))

        if report.total_projects > 0 and report.first_attempt_success_rate < self.ALERT_HIGH_RETRY_RATE:
            alerts.append(Alert(
                name="high_retry_rate",
                severity="warning",
                message=(
                    f"First-attempt success rate ({report.first_attempt_success_rate:.1%}) "
                    f"is below threshold ({self.ALERT_HIGH_RETRY_RATE:.0%})"
                ),
                current_value=report.first_attempt_success_rate,
                threshold=self.ALERT_HIGH_RETRY_RATE,
            ))

        try:
            cost_alert = await self._check_cost_spike(report)
            if cost_alert:
                alerts.append(cost_alert)
        except Exception:
            logger.exception("Failed to check cost spike alert")

        return alerts

    async def get_optimization_suggestions(
        self, report: TokenUsageReport
    ) -> list[str]:
        suggestions: list[str] = []

        if report.cache_hit_rate < 0.20:
            suggestions.append(
                "Cache hit rate is low. Consider expanding fingerprint matching "
                "to cover more common project configurations."
            )

        if report.first_attempt_success_rate < 0.70:
            suggestions.append(
                "First-attempt success rate is below 70%. Review the most common "
                "build errors and add template coverage for those frameworks."
            )

        if report.avg_tokens_per_project > 25_000:
            suggestions.append(
                "Average token usage is high. Consider using Gemini Flash for "
                "error-fix iterations and reserving Pro for initial analysis."
            )

        pro_data = report.per_model_breakdown.get("gemini-2.5-pro", {})
        flash_data = report.per_model_breakdown.get("gemini-2.5-flash", {})
        pro_tokens = pro_data.get("total_tokens", 0)
        flash_tokens = flash_data.get("total_tokens", 0)
        total = pro_tokens + flash_tokens
        if total > 0 and pro_tokens / total > 0.7:
            suggestions.append(
                "Over 70% of tokens are consumed by Pro model. "
                "Shift error-fix and retry steps to Flash to reduce costs."
            )

        fix_tokens = report.per_step_breakdown.get("fix", {}).get("total_tokens", 0)
        if total > 0 and fix_tokens / total > 0.5:
            suggestions.append(
                "Fix/retry steps consume over 50% of total tokens. "
                "Improve initial Dockerfile quality to reduce retry loops."
            )

        if not suggestions:
            suggestions.append("No immediate optimisation opportunities detected.")

        return suggestions

    # --- private helpers ---

    @staticmethod
    async def _count_projects(
        db: AsyncSession, start: datetime, end: datetime
    ) -> int:
        result = await db.execute(
            select(func.count(Project.id)).where(
                Project.created_at >= start,
                Project.created_at < end,
            )
        )
        return result.scalar() or 0

    @staticmethod
    async def _get_interactions(
        db: AsyncSession, start: datetime, end: datetime
    ) -> list[AIInteraction]:
        result = await db.execute(
            select(AIInteraction).where(
                AIInteraction.created_at >= start,
                AIInteraction.created_at < end,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def _get_builds(
        db: AsyncSession, start: datetime, end: datetime
    ) -> list[Build]:
        result = await db.execute(
            select(Build).where(
                Build.created_at >= start,
                Build.created_at < end,
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def _top_token_consumers(
        db: AsyncSession, start: datetime, end: datetime, n: int = 10
    ) -> list[dict[str, Any]]:
        stmt = (
            select(
                AIInteraction.project_id,
                func.sum(
                    AIInteraction.prompt_tokens + AIInteraction.completion_tokens
                ).label("total_tokens"),
                func.count(AIInteraction.id).label("interaction_count"),
            )
            .where(
                AIInteraction.created_at >= start,
                AIInteraction.created_at < end,
            )
            .group_by(AIInteraction.project_id)
            .order_by(func.sum(AIInteraction.prompt_tokens + AIInteraction.completion_tokens).desc())
            .limit(n)
        )
        result = await db.execute(stmt)
        return [
            {
                "project_id": str(row.project_id),
                "total_tokens": int(row.total_tokens or 0),
                "interaction_count": int(row.interaction_count or 0),
            }
            for row in result.all()
        ]

    @staticmethod
    def _calculate_cost(interactions: list[AIInteraction]) -> float:
        total = 0.0
        for i in interactions:
            tokens = (i.prompt_tokens or 0) + (i.completion_tokens or 0)
            model = i.model_used or ""
            rate = COST_PER_MILLION_TOKENS.get(model, 0.30)
            total += tokens / 1_000_000 * rate
        return total

    @staticmethod
    def _per_model_breakdown(
        interactions: list[AIInteraction],
    ) -> dict[str, Any]:
        breakdown: dict[str, dict[str, Any]] = {}
        for i in interactions:
            model = i.model_used or "unknown"
            entry = breakdown.setdefault(model, {"total_tokens": 0, "count": 0, "cost_usd": 0.0})
            tokens = (i.prompt_tokens or 0) + (i.completion_tokens or 0)
            entry["total_tokens"] += tokens
            entry["count"] += 1
            rate = COST_PER_MILLION_TOKENS.get(model, 0.30)
            entry["cost_usd"] += tokens / 1_000_000 * rate
        for v in breakdown.values():
            v["cost_usd"] = round(v["cost_usd"], 6)
        return breakdown

    @staticmethod
    def _per_step_breakdown(
        interactions: list[AIInteraction],
    ) -> dict[str, Any]:
        breakdown: dict[str, dict[str, Any]] = {}
        for i in interactions:
            step = i.interaction_type or "unknown"
            entry = breakdown.setdefault(step, {"total_tokens": 0, "count": 0})
            tokens = (i.prompt_tokens or 0) + (i.completion_tokens or 0)
            entry["total_tokens"] += tokens
            entry["count"] += 1
        return breakdown

    @staticmethod
    def _first_attempt_success_rate(builds: list[Build]) -> float:
        first_attempts = [b for b in builds if b.attempt_number == 1]
        if not first_attempts:
            return 0.0
        successes = sum(1 for b in first_attempts if b.build_status == "success")
        return successes / len(first_attempts)

    async def _check_cost_spike(self, report: TokenUsageReport) -> Alert | None:
        now = datetime.now(timezone.utc)
        today_cost = report.total_cost_usd

        async with async_session_factory() as db:
            seven_days_ago = now - timedelta(days=7)
            result = await db.execute(
                select(
                    func.sum(
                        AIInteraction.prompt_tokens + AIInteraction.completion_tokens
                    )
                ).where(
                    AIInteraction.created_at >= seven_days_ago,
                    AIInteraction.created_at < now,
                )
            )
            total_7d_tokens = result.scalar() or 0

        avg_daily_cost = (total_7d_tokens / 1_000_000 * 0.30) / 7.0
        if avg_daily_cost > 0 and today_cost > avg_daily_cost * self.ALERT_COST_SPIKE_MULTIPLIER:
            return Alert(
                name="cost_spike",
                severity="critical",
                message=(
                    f"Current period cost (${today_cost:.4f}) is >{self.ALERT_COST_SPIKE_MULTIPLIER}x "
                    f"the 7-day daily average (${avg_daily_cost:.4f})"
                ),
                current_value=today_cost,
                threshold=round(avg_daily_cost * self.ALERT_COST_SPIKE_MULTIPLIER, 6),
            )
        return None
