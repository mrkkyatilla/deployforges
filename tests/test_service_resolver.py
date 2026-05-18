"""T0: service list resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.analysis.engine import AnalysisEngine
from core.analysis.service_resolver import pick_primary, resolve_services

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.asyncio
async def test_monorepo_fullstack_resolves_multiple_services() -> None:
    root = FIXTURES / "monorepo_fullstack"
    engine = AnalysisEngine()
    fp = (await engine.analyze(str(root))).to_dict()
    services, order = resolve_services(str(root), fp)
    names = {s.name for s in services}
    assert len(services) >= 2
    assert order
    primary = pick_primary(services)
    assert primary in names
