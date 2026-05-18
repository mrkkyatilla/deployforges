"""T0: per-service analysis."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.analysis.engine import AnalysisEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.asyncio
async def test_analyze_service_backend() -> None:
    root = FIXTURES / "monorepo_fullstack"
    engine = AnalysisEngine()
    fp = await engine.analyze_service(str(root), "backend", service_name="backend")
    assert fp.language.primary == "python"
    assert fp.environment.get("parent_monorepo") is True
    assert fp.environment.get("service_name") == "backend"
