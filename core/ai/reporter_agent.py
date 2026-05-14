"""Admin-only usage reporter: deterministic metrics plus optional Gemini summary."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from api.config import settings
from core.ai.gemini_client import GeminiClient
from core.ai.gemini_schemas import schema_reporter_analysis
from core.ai.json_response import parse_model_json_from_ai_response
from core.monitoring import TokenMonitor
from db.models import AIInteraction
from db.session import async_session_factory

logger = logging.getLogger(__name__)

REPORTER_LLM_SYSTEM = """\
You are a platform reliability analyst for DeployForge. You receive ONLY aggregate JSON: \
token counts, model mix, interaction types, optional parse_ok rates — no customer code, no \
Dockerfiles, no secrets. Write concise operational guidance for administrators. \
Respond with JSON only matching the response schema.
"""


async def _interaction_type_breakdown(
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    async with async_session_factory() as db:
        stmt = (
            select(
                AIInteraction.interaction_type,
                func.count(AIInteraction.id).label("n"),
                func.coalesce(
                    func.sum(
                        AIInteraction.prompt_tokens + AIInteraction.completion_tokens
                    ),
                    0,
                ).label("tokens"),
            )
            .where(
                AIInteraction.created_at >= start,
                AIInteraction.created_at < end,
            )
            .group_by(AIInteraction.interaction_type)
        )
        result = await db.execute(stmt)
        rows = result.all()
    by_type: dict[str, dict[str, int]] = {}
    for row in rows:
        key = row.interaction_type or "unknown"
        by_type[key] = {"count": int(row.n or 0), "total_tokens": int(row.tokens or 0)}
    return {"by_interaction_type": by_type}


async def _parse_ok_sample_stats(
    start: datetime,
    end: datetime,
    sample_limit: int = 2000,
) -> dict[str, Any]:
    """Approximate parse_ok rates from persisted ``extra`` (no PII)."""
    async with async_session_factory() as db:
        stmt = (
            select(AIInteraction.extra)
            .where(
                AIInteraction.created_at >= start,
                AIInteraction.created_at < end,
                AIInteraction.extra.is_not(None),
            )
            .limit(sample_limit)
        )
        result = await db.execute(stmt)
        extras = [r[0] for r in result.all() if r[0]]

    if not extras:
        return {"sampled_rows": 0, "parse_ok_true": 0, "parse_ok_false": 0, "parse_ok_unknown": 0}

    ok_t = ok_f = unk = 0
    for ex in extras:
        if not isinstance(ex, dict):
            unk += 1
            continue
        v = ex.get("parse_ok")
        if v is True:
            ok_t += 1
        elif v is False:
            ok_f += 1
        else:
            unk += 1
    return {
        "sampled_rows": len(extras),
        "parse_ok_true": ok_t,
        "parse_ok_false": ok_f,
        "parse_ok_unknown": unk,
    }


def _report_to_dict(report: Any) -> dict[str, Any]:
    from core.monitoring import TokenUsageReport

    if not isinstance(report, TokenUsageReport):
        return {}
    return {
        "period": report.period,
        "total_projects": report.total_projects,
        "total_tokens": report.total_tokens,
        "total_cost_usd": report.total_cost_usd,
        "per_model_breakdown": report.per_model_breakdown,
        "per_step_breakdown": report.per_step_breakdown,
        "avg_tokens_per_project": report.avg_tokens_per_project,
        "avg_cost_per_project": report.avg_cost_per_project,
        "cache_hit_rate": report.cache_hit_rate,
        "first_attempt_success_rate": report.first_attempt_success_rate,
        "top_token_consumers": report.top_token_consumers[:10],
        "alerts": [asdict(a) for a in report.alerts],
    }


async def run_reporter_report(
    *,
    period_days: int = 7,
    include_llm: bool = False,
) -> dict[str, Any]:
    """Build deterministic report + optional structured LLM narrative (aggregate input only)."""
    now = datetime.now(UTC)
    period_start = now - timedelta(days=period_days)

    monitor = TokenMonitor()
    report = await monitor.generate_report(period_start, now)
    suggestions = await monitor.get_optimization_suggestions(report)

    aggregates = {
        "interaction_breakdown": await _interaction_type_breakdown(period_start, now),
        "extra_parse_sample": await _parse_ok_sample_stats(period_start, now),
    }

    deterministic: dict[str, Any] = {
        "period_days": period_days,
        "period_start": period_start.isoformat(),
        "period_end": now.isoformat(),
        "report": _report_to_dict(report),
        "suggestions": suggestions,
        "aggregates": aggregates,
    }

    want_llm = settings.reporter_llm_enabled and bool(include_llm)
    if not want_llm:
        deterministic["llm_enabled"] = False
        return {"deterministic": deterministic, "llm": None}

    if not (settings.gemini_api_key or "").strip():
        logger.warning("Reporter LLM skipped: no Gemini API key")
        deterministic["llm_enabled"] = False
        deterministic["llm_skip_reason"] = "no_gemini_api_key"
        return {"deterministic": deterministic, "llm": None}

    payload = {
        "metrics": deterministic["report"],
        "suggestions": suggestions[:20],
        "aggregates": aggregates,
    }
    prompt = (
        "## Aggregate metrics (JSON)\n```json\n"
        f"{json.dumps(payload, default=str)[:24000]}\n```\n"
        "## Task\nProduce the structured admin summary fields per system instructions."
    )

    client = GeminiClient()
    response = await client.generate_json(
        prompt=prompt,
        system_instruction=REPORTER_LLM_SYSTEM,
        model=settings.gemini_flash_model,
        max_output_tokens=2048,
        response_schema=schema_reporter_analysis(),
        io_log_label="admin_reporter",
    )
    pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
    llm_out: dict[str, Any] | None = pr.data if isinstance(pr.data, dict) else None
    if llm_out is None:
        logger.warning("Reporter LLM parse failed: %s", pr.error)
        deterministic["llm_enabled"] = True
        deterministic["llm_parse_error"] = pr.error
        return {
            "deterministic": deterministic,
            "llm": None,
            "llm_raw_excerpt": response.text[:2000] if response.text else None,
        }

    deterministic["llm_enabled"] = True
    return {
        "deterministic": deterministic,
        "llm": llm_out,
        "llm_tokens": response.total_tokens,
    }
