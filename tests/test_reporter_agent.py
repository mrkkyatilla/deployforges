"""Unit tests for admin reporter helpers."""

from __future__ import annotations

from core.ai.reporter_agent import _report_to_dict
from core.monitoring import Alert, TokenUsageReport


def test_report_to_dict_serializes_alerts() -> None:
    report = TokenUsageReport(
        period="2026-01-01 — 2026-01-02",
        total_projects=2,
        total_tokens=500,
        total_cost_usd=0.02,
        per_model_breakdown={"gemini-2.5-flash": {"total_tokens": 500, "count": 2, "cost_usd": 0.02}},
        per_step_breakdown={"generation": {"total_tokens": 400, "count": 1}},
        avg_tokens_per_project=250.0,
        avg_cost_per_project=0.01,
        cache_hit_rate=0.1,
        first_attempt_success_rate=0.8,
        top_token_consumers=[{"project_id": "00000000-0000-0000-0000-000000000001", "total_tokens": 500, "interaction_count": 2}],
        alerts=[Alert("high_token_usage", "warning", "msg", 30_001.0, 30_000.0)],
    )
    d = _report_to_dict(report)
    assert d["total_tokens"] == 500
    assert d["total_projects"] == 2
    assert len(d["alerts"]) == 1
    assert d["alerts"][0]["name"] == "high_token_usage"
