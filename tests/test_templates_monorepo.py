"""T0: template-first for monorepo service roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ai.templates import fingerprint_allows_template_first, render_template_dockerfile
from core.analysis.engine import AnalysisEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.asyncio
async def test_backend_template_first() -> None:
    root = FIXTURES / "monorepo_fullstack"
    engine = AnalysisEngine()
    fp = await engine.analyze_service(str(root), "backend", service_name="backend")
    fp_d = fp.to_dict()
    assert fingerprint_allows_template_first(str(root), fp_d, "backend")
    df = render_template_dockerfile(str(root), fp_d, service_root="backend")
    assert df and "FROM" in df.upper()
